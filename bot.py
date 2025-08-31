# bot.py — Telegram 客服 + 账本机器人（Aiogram v3 + OpenAI Responses API）
# Features:
# - Per-group language: "set version en|cn", "version" to display
# - Account lines localized (tiers show Large/Mid/Small in EN groups; stored as 大额户/中额户/小额户)
# - Broadcast to all groups: "broadcast <message>" (OWNER/ALLOWED only), groups tracked in chats.txt
# - Ledger per group in ./bill/ (TXT, FIFO payout), overview & detail, +/- adjustments, payout
# - Account management (add/list/filter, set tier/status, set/add amount)
# - Shout to pick account by inferred amount tier; mention bot to chat with GPT

import os
import re
import asyncio
import logging
from typing import Optional, List, Dict, Tuple, Any, Set
from collections import deque
from zoneinfo import ZoneInfo
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
import httpx

# --------- KEYS ----------
TELEGRAM_TOKEN = "8034458767:AAEPeLJlP_XOksWyiWSYFDcpj9SOAQbTN3w"
OPENAI_API_KEY = "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"
OPENAI_MODEL = "gpt-5-mini"
# -------------------------

# --------- PERMISSIONS ----------
OWNER_ID = 7681963841
ALLOWED_USERS = {7825042384, 7449394947, 7681963841, 7983854144, 8172982320}
# -------------------------------

# --------- FILES ----------
ACCOUNTS_FILE = "accounts.txt"
BILLS_DIR = "bill"
LANG_FILE = "lang.txt"       # chat_id=en|cn
CHATS_FILE = "chats.txt"     # for broadcast targets
os.makedirs(BILLS_DIR, exist_ok=True)
for path in (LANG_FILE, CHATS_FILE):
    if not os.path.isfile(path):
        open(path, "a").close()
# -------------------------

# --------- GPT SHORT MEMORY ----------
MEMORY: Dict[int, deque] = {}
MAX_MEMORY = 5
# -------------------------------------

TZ = ZoneInfo("America/Toronto")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("support-ledger-bot")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# =================== Language / i18n ===================

I18N: Dict[str, Dict[str, str]] = {
    "start": {
        "cn": "你好，我是‘枫枫’💖\n• 群里喊单：e转 100 / emt800 / e 转 1,200（自动分档贴卡，仅贴 Active）\n• @我 与我对话\n• 输入 command 查看所有命令\n• 可用 set version en/cn 切换语言；version 查看当前语言\n• 账本保存在 ./bill/，按群独立文件。",
        "en": "Hi, I’m Maple 💖\n• In group: say e-transfer like “e转 100 / emt800 / e 1,200” and I’ll pick a matching account (Active only)\n• Mention me to chat: @YourBot\n• Type command to see all commands\n• Use set version en/cn to switch language; use version to show current language\n• Each group has its own ledger in ./bill/."
    },
    "help": {
        "cn": (
            "📖 全部命令\n"
            "【广播】\n"
            "  broadcast <内容>           —— 向所有群发送群发（仅白名单）\n"
            "【账本】\n"
            "  command                    —— 显示本帮助\n"
            "  bill                       —— 查看账单概览\n"
            "  bill detail                —— 查看逐笔交易（含结清状态）\n"
            "  bill + 金额 [备注]          —— 正向调整（入账型 ADJ）\n"
            "  bill - 金额 [备注]          —— 负向调整（出账型 ADJ）\n"
            "  payout 金额 [备注]          —— 记录回款（PAY，按FIFO冲抵）\n"
            "【账户】\n"
            "  list accounts [筛选]        —— 大额户/中额户/小额户/active/inactive\n"
            "  list active / list inactive —— 仅显示 Active / Inactive\n"
            "  add account <完整一行>\n"
            "  set tier #001 大额户 | set status #001 Active\n"
            "  set amount #001 3500 | add amount #001 +200\n"
            "【语言】\n"
            "  set version en|cn          —— 设定群语言（管理员）\n"
            "  version                    —— 查看当前语言\n"
            "注：修改类命令仅限白名单管理员；金额默认 CAD；时间 America/Toronto。"
        ),
        "en": (
            "📖 All Commands\n"
            "[Broadcast]\n"
            "  broadcast <message>        — send to all groups (admin only)\n"
            "[Ledger]\n"
            "  command                    — show this help\n"
            "  bill                       — overview\n"
            "  bill detail                — list all entries (settled/pending)\n"
            "  bill + amount [note]       — positive adjustment (ADJ)\n"
            "  bill - amount [note]       — negative adjustment (ADJ)\n"
            "  payout amount [note]       — record payout (PAY, FIFO offsets)\n"
            "[Accounts]\n"
            "  list accounts [filter]     — filter: 大额户/中额户/小额户/active/inactive\n"
            "  list active / list inactive\n"
            "  add account <full line>\n"
            "  set tier #001 大额户 | set status #001 Active\n"
            "  set amount #001 3500 | add amount #001 +200\n"
            "[Language]\n"
            "  set version en|cn          — set group language (admin)\n"
            "  version                    — show current language\n"
            "Note: modifying commands require admin; currency CAD; timezone America/Toronto."
        ),
    },
    "overview_title": {"cn": "📊 本群账单概览", "en": "📊 Group Ledger Overview"},
    "trade_amount": {"cn": "已交易金额", "en": "Total Trade Amount"},
    "trade_count": {"cn": "已交易笔数", "en": "Total Trade Count"},
    "pending_amount": {"cn": "待回款金额", "en": "Pending Payout Amount"},
    "pending_count": {"cn": "待回款笔数", "en": "Pending Entries"},
    "detail_title": {"cn": "📜 交易明细（按录入先后）", "en": "📜 Ledger Details (by time)"},
    "note": {"cn": "备注", "en": "Note"},
    "by": {"cn": "操作者", "en": "by"},
    "status_settled": {"cn": "✅已结清", "en": "✅ Settled"},
    "status_pending": {"cn": "⏳未结（剩余 ${rem}）", "en": "⏳ Pending (remain ${rem})"},
    "err_amount": {"cn": "❌ 金额格式错误。例：{ex}", "en": "❌ Invalid amount. e.g. {ex}"},
    "no_account_tier": {"cn": "❌ 暂无可用 {tier}（Active）账户，请联系管理员。", "en": "❌ No available {tier} (Active) account. Please contact admin."},
    "reply_account": {"cn": "{acct}", "en": "{acct}"},
    "version_now": {"cn": "当前语言：{lang}", "en": "Current language: {lang}"},
    "version_set_ok": {"cn": "✅ 已设置本群语言为：{lang}", "en": "✅ Group language set to: {lang}"},
    "not_admin": {"cn": "只有管理员可执行此命令。", "en": "Only admins can do this."},
    "broadcast_done": {"cn": "📣 已向 {ok}/{total} 个群发送。", "en": "📣 Broadcast sent to {ok}/{total} groups."},
}

LANG_LABEL = {"cn": "中文", "en": "English"}

def load_lang_map() -> Dict[int, str]:
    m: Dict[int, str] = {}
    with open(LANG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            cid_s, val = line.split("=", 1)
            try:
                m[int(cid_s)] = "en" if val.strip().lower() == "en" else "cn"
            except:
                pass
    return m

def save_lang_map(m: Dict[int, str]):
    with open(LANG_FILE, "w", encoding="utf-8") as f:
        for k, v in m.items():
            f.write(f"{k}={v}\n")

def get_lang(chat_id: int) -> str:
    m = load_lang_map()
    return m.get(chat_id, "cn")

def set_lang(chat_id: int, v: str):
    v = "en" if v.lower() == "en" else "cn"
    m = load_lang_map()
    m[chat_id] = v
    save_lang_map(m)

def t(key: str, chat_id: int, **kw) -> str:
    lang = get_lang(chat_id)
    s = I18N.get(key, {}).get(lang, "")
    for k, v in kw.items():
        s = s.replace("${"+k+"}", str(v)).replace("{"+k+"}", str(v))
    return s or key

# =================== Broadcast helpers ===================

def load_chat_ids() -> Set[int]:
    ids: Set[int] = set()
    try:
        with open(CHATS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(int(line))
                except:
                    pass
    except FileNotFoundError:
        pass
    return ids

def add_chat_id(chat_id: int) -> None:
    ids = load_chat_ids()
    if chat_id not in ids:
        with open(CHATS_FILE, "a", encoding="utf-8") as f:
            f.write(str(chat_id) + "\n")
        log.info(f"New group recorded for broadcast: {chat_id}")

async def do_broadcast(message: str) -> Tuple[int, int]:
    ids = list(load_chat_ids())
    ok = 0
    for chat_id in ids:
        try:
            await bot.send_message(chat_id, f"📣 {message}")
            ok += 1
        except Exception as e:
            log.warning(f"Broadcast to {chat_id} failed: {e}")
        await asyncio.sleep(0.06)  # gentle throttle
    return ok, len(ids)

# =================== Accounts (with localized rendering) ===================

LINE_RE_NEW = re.compile(
    r"^\s*#(?P<id>\d+)\s*-\s*(?P<name>[^-]+?)\s*-\s*(?P<email>[^-]+?)\s*-\s*"
    r"(?P<status>Active|Inactive)\s*-\s*(?P<tier>大额户|中额户|小额户)\s*-\s*(?P<amount>[-+]?\d+(?:\.\d+)?)\s*$"
)
LINE_RE_OLD = re.compile(
    r"^\s*#(?P<id>\d+)\s*-\s*(?P<name>[^-]+?)\s*-\s*(?P<email>[^-]+?)\s*-\s*"
    r"(?P<status>Active|Inactive)\s*-\s*(?P<tier>大额户|中额户|小额户)\s*$"
)

TIERS = ("大额户", "中额户", "小额户")
TIER_DISPLAY_EN = {"大额户": "Large", "中额户": "Mid", "小额户": "Small"}

def parse_account_line(line: str) -> Optional[Dict[str, Any]]:
    m = LINE_RE_NEW.match(line)
    if m:
        d = m.groupdict()
        return {
            "id": d["id"].strip(),
            "name": d["name"].strip(),
            "email": d["email"].strip(),
            "status": d["status"].strip(),
            "tier": d["tier"].strip(),   # stored in Chinese
            "amount": float(d["amount"]),
        }
    m2 = LINE_RE_OLD.match(line)
    if m2:
        d = m2.groupdict()
        return {
            "id": d["id"].strip(),
            "name": d["name"].strip(),
            "email": d["email"].strip(),
            "status": d["status"].strip(),
            "tier": d["tier"].strip(),
            "amount": 0.0,
        }
    return None

def load_account_objs() -> List[Dict[str, Any]]:
    if not os.path.isfile(ACCOUNTS_FILE):
        open(ACCOUNTS_FILE, "w").close()
    objs: List[Dict[str, Any]] = []
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            obj = parse_account_line(line)
            if obj:
                objs.append(obj)
    return objs

def fmt_amount(v: float) -> str:
    return str(int(v)) if abs(v - int(v)) < 1e-9 else f"{v:.2f}"

def write_account_objs(objs: List[Dict[str, Any]]):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        for o in objs:
            f.write(
                f"#{o['id']} - {o['name']} - {o['email']} - {o['status']} - {o['tier']} - {fmt_amount(float(o['amount']))}\n"
            )

def add_account_line(line: str) -> Tuple[bool, str]:
    obj = parse_account_line(line)
    if not obj:
        return False, "❌ 格式错误。示例：#001 - Name - email@example.com - Active - 大额户 - 0"
    objs = load_account_objs()
    if any(o["id"] == obj["id"] for o in objs):
        return False, f"❌ 编号已存在：#{obj['id']}"
    objs.append(obj)
    write_account_objs(objs)
    return True, f"✅ 已添加账户：#{obj['id']} - {obj['name']}"

def format_account_line(o: dict, chat_id: int) -> str:
    lang = get_lang(chat_id)
    tier_label = TIER_DISPLAY_EN.get(o["tier"], o["tier"]) if lang == "en" else o["tier"]
    return f"#{o['id']} - {o['name']} - {o['email']} - {o['status']} - {tier_label} - {fmt_amount(float(o['amount']))}"

def list_accounts_text(filter_key: Optional[str], chat_id: int) -> str:
    objs = load_account_objs()
    if filter_key:
        k = filter_key.strip().lower()
        if k in ("active", "inactive"):
            objs = [o for o in objs if o["status"].lower() == k]
        elif filter_key in TIERS:
            objs = [o for o in objs if o["tier"] == filter_key]
    lines = [format_account_line(o, chat_id) for o in objs]
    return "\n".join(lines) if lines else ("（暂无账户）" if get_lang(chat_id) == "cn" else "(No accounts)")

def set_account_tier(acct_id: str, new_tier: str) -> Tuple[bool, str]:
    if new_tier not in TIERS:
        return False, "❌ 档位仅支持：大额户 / 中额户 / 小额户"
    objs = load_account_objs()
    hit = False
    for o in objs:
        if o["id"] == acct_id:
            o["tier"] = new_tier
            hit = True
            break
    if not hit:
        return False, f"❌ 未找到账户：#{acct_id}"
    write_account_objs(objs)
    return True, f"✅ 已将 #{acct_id} 档位改为：{new_tier}"

def set_account_status(acct_id: str, new_status: str) -> Tuple[bool, str]:
    ns = new_status.capitalize()
    if ns not in ("Active", "Inactive"):
        return False, "❌ 状态仅支持：Active / Inactive"
    objs = load_account_objs()
    hit = False
    for o in objs:
        if o["id"] == acct_id:
            o["status"] = ns
            hit = True
            break
    if not hit:
        return False, f"❌ 未找到账户：#{acct_id}"
    write_account_objs(objs)
    return True, f"✅ 已将 #{acct_id} 状态改为：{ns}"

def set_account_amount(acct_id: str, new_amount: float) -> Tuple[bool, str]:
    objs = load_account_objs()
    hit = False
    for o in objs:
        if o["id"] == acct_id:
            o["amount"] = float(new_amount)
            hit = True
            break
    if not hit:
        return False, f"❌ 未找到账户：#{acct_id}"
    write_account_objs(objs)
    return True, f"✅ 已将 #{acct_id} 已收款金额设为：{fmt_amount(float(new_amount))}"

def add_account_amount(acct_id: str, delta: float) -> Tuple[bool, str]:
    objs = load_account_objs()
    hit = False
    new_val = None
    for o in objs:
        if o["id"] == acct_id:
            o["amount"] = float(o["amount"]) + float(delta)
            new_val = o["amount"]
            hit = True
            break
    if not hit:
        return False, f"❌ 未找到账户：#{acct_id}"
    write_account_objs(objs)
    sign = "+" if delta >= 0 else ""
    return True, f"✅ 已将 #{acct_id} 金额变更 {sign}{fmt_amount(float(delta))}，现为 {fmt_amount(float(new_val))}"

# Round-robin pointer per tier
RR_INDEX_BY_TIER: Dict[str, int] = {"大额户": 0, "中额户": 0, "小额户": 0}

def choose_account_by_tier(tier: str, chat_id: int) -> Optional[str]:
    objs = [o for o in load_account_objs() if o["status"] == "Active" and o["tier"] == tier]
    if not objs:
        return None
    i = RR_INDEX_BY_TIER.get(tier, 0) % len(objs)
    RR_INDEX_BY_TIER[tier] = (i + 1) % len(objs)
    return format_account_line(objs[i], chat_id)

# =================== GPT memory/call ===================

def add_memory(chat_id: int, role: str, content: str):
    if chat_id not in MEMORY:
        MEMORY[chat_id] = deque(maxlen=MAX_MEMORY)
    MEMORY[chat_id].append({"role": role, "content": content})

def get_memory(chat_id: int):
    return list(MEMORY.get(chat_id, []))

async def ask_openai(question: str, *, chat_id: int) -> str:
    system_prompt = (
        "你是‘枫枫’，一个友善可爱的客服。回答简洁自然。"
        "遇到账户/账本/广播/语言设置相关命令不要自行回答，由代码处理。"
    )
    history = get_memory(chat_id)
    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": question}
        ]
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post("https://api.openai.com/v1/responses", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    if "output_text" in data and data["output_text"]:
        return data["output_text"].strip()
    return "⚠️ AI 没有返回有效内容"

# =================== Amount → Tier mapping ===================

NUM_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", re.U)

def extract_amount(text: str) -> Optional[float]:
    compact = text.replace(" ", "")
    m = NUM_RE.search(compact)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        return float(raw)
    except Exception:
        return None

def map_amount_to_tier(amt: Optional[float]) -> str:
    if amt is None:
        return "小额户"
    if amt >= 1000:
        return "大额户"
    if 500 < amt < 1000:
        return "中额户"
    return "小额户"

# =================== Ledger (per group, saved in ./bill/) ===================

def ledger_path(chat_id: int) -> str:
    return os.path.join(BILLS_DIR, f"ledger_{chat_id}.txt")

def now_ts() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

def append_ledger(chat_id: int, entry: Dict[str, Any]):
    line = f"{entry['time']} | {entry['type']} | {entry['amount']:+.2f} | {entry['id']} | by={entry['user']} | chat={chat_id} | note={entry['note']}"
    with open(ledger_path(chat_id), "a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_ledger(chat_id: int) -> List[Dict[str, Any]]:
    path = ledger_path(chat_id)
    if not os.path.isfile(path):
        open(path, "a").close()
    entries: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 4:
                continue
            time_str = parts[0]
            typ = parts[1]
            amt_str = parts[2].replace("+", "")
            eid = parts[3]
            meta = " | ".join(parts[4:]) if len(parts) > 4 else ""
            user = ""
            note = ""
            chat = ""
            m_user = re.search(r"by=(.+?)(?:\s*\|\s*|$)", meta)
            if m_user: user = m_user.group(1).strip()
            m_chat = re.search(r"chat=([-\d]+)", meta)
            if m_chat: chat = m_chat.group(1).strip()
            m_note = re.search(r"note=(.*)$", meta)
            if m_note: note = m_note.group(1).strip()
            try:
                amount = float(amt_str)
            except:
                continue
            entries.append({
                "time": time_str, "type": typ, "amount": amount, "id": eid,
                "user": user, "chat": chat, "note": note
            })
    return entries

def next_id(entries: List[Dict[str, Any]], prefix: str) -> str:
    n = sum(1 for e in entries if e["id"].startswith(prefix))
    return f"{prefix}-{n+1:04d}"

def fifo_simulate(entries: List[Dict[str, Any]]) -> Tuple[float, int, float, int, Dict[str, float]]:
    total_trade_amount = 0.0
    total_trade_count = 0
    positive_pool: List[Dict[str, Any]] = []
    remaining_per_pos: Dict[str, float] = {}
    for e in entries:
        if e["type"] in ("TXN", "ADJ"):
            total_trade_amount += e["amount"]
            total_trade_count += 1
        if e["type"] in ("TXN", "ADJ") and e["amount"] > 0:
            positive_pool.append({"id": e["id"], "remaining": e["amount"]})
            remaining_per_pos[e["id"]] = e["amount"]

    def deduct(amount: float):
        nonlocal positive_pool, remaining_per_pos
        need = amount
        for item in positive_pool:
            if need <= 0:
                break
            take = min(item["remaining"], need)
            item["remaining"] -= take
            remaining_per_pos[item["id"]] -= take
            need -= take
        positive_pool = [it for it in positive_pool if it["remaining"] > 1e-9]

    for e in entries:
        if e["type"] == "ADJ" and e["amount"] < 0:
            deduct(-e["amount"])
        elif e["type"] == "PAY":
            deduct(e["amount"])

    pending_amount = sum(it["remaining"] for it in positive_pool)
    pending_count = len(positive_pool)
    for k in list(remaining_per_pos.keys()):
        if abs(remaining_per_pos[k]) < 1e-9:
            remaining_per_pos[k] = 0.0
    return total_trade_amount, total_trade_count, pending_amount, pending_count, remaining_per_pos

def format_overview(chat_id: int) -> str:
    entries = load_ledger(chat_id)
    total_amt, total_cnt, pend_amt, pend_cnt, _ = fifo_simulate(entries)
    return (
        f"{t('overview_title',chat_id)}\n"
        f"{t('trade_amount',chat_id)}：${total_amt:.2f}\n"
        f"{t('trade_count',chat_id)}：{total_cnt}\n"
        f"{t('pending_amount',chat_id)}：${pend_amt:.2f}\n"
        f"{t('pending_count',chat_id)}：{pend_cnt}"
    )

def format_detail(chat_id: int) -> str:
    entries = load_ledger(chat_id)
    _, _, _, _, remaining = fifo_simulate(entries)
    lines = [t("detail_title", chat_id)]
    for e in entries:
        base = f"{e['time']} | {e['id']} | {e['type']} | {e['amount']:+.2f} | {t('note',chat_id)}：{e['note'] or '-'} | {t('by',chat_id)}:{e['user']}"
        if e["type"] in ("TXN", "ADJ") and e["amount"] > 0:
            rem = remaining.get(e["id"], 0.0)
            status = t('status_settled', chat_id) if rem == 0 else t('status_pending', chat_id, rem=f'${rem:.2f}')
            lines.append(base + " | " + status)
        else:
            lines.append(base)
    text = "\n".join(lines)
    if len(text) > 3500:
        text = "\n".join(lines[:100]) + "\n…"
    return text

def record_entry(chat_id: int, typ: str, amount: float, user: str, note: str) -> Tuple[bool, str]:
    entries = load_ledger(chat_id)
    if typ == "TXN":
        eid = next_id(entries, "TXN")
    elif typ == "ADJ":
        eid = next_id(entries, "ADJ")
    elif typ == "PAY":
        eid = next_id(entries, "PAY")
    else:
        return False, "❌ 未知类型"
    entry = {"time": now_ts(), "type": typ, "amount": amount, "id": eid, "user": user, "note": note or ""}
    append_ledger(chat_id, entry)
    return True, eid

def parse_amount_and_note(text_after_cmd: str) -> Tuple[Optional[float], str]:
    s = text_after_cmd.strip()
    m = re.match(r"^([-+]?\d+(?:\.\d+)?)(?:\s+(.+))?$", s)
    if not m:
        return None, ""
    amt = float(m.group(1))
    note = (m.group(2) or "").strip()
    return amt, note

# =================== Handlers ===================

@router.message(Command("start"))
async def on_start(msg: types.Message):
    await msg.reply(t("start", msg.chat.id))

@router.message(F.chat.type.in_({"group","supergroup"}))
async def on_group(msg: types.Message):
    text = (msg.text or "").strip()
    if not text:
        return

    # record this group for future broadcasts
    add_chat_id(msg.chat.id)

    uid = msg.from_user.id
    is_admin = (uid == OWNER_ID) or (uid in ALLOWED_USERS)
    chat_id = msg.chat.id
    user_tag = f"{msg.from_user.full_name}({uid})"
    low = text.lower()

    # language control
    if low == "version":
        lang = get_lang(chat_id)
        await msg.reply(t("version_now", chat_id, lang=LANG_LABEL[lang])); return
    m_setv = re.match(r"^\s*set\s+version\s+(en|cn)\s*$", low)
    if m_setv:
        if not is_admin:
            await msg.reply(t("not_admin", chat_id)); return
        set_lang(chat_id, m_setv.group(1))
        await msg.reply(t("version_set_ok", chat_id, lang=LANG_LABEL[get_lang(chat_id)])); return

    # help
    if low == "command":
        await msg.reply(I18N["help"][get_lang(chat_id)]); return

    # broadcast (admin only)
    if is_admin and low.startswith("broadcast"):
        payload = text[len("broadcast"):].strip()
        if not payload:
            await msg.reply("用法 / Usage：broadcast <message>")
            return
        ok, total = await do_broadcast(payload)
        await msg.reply(t("broadcast_done", chat_id, ok=ok, total=total)); return

    # admin account commands
    if is_admin:
        if low == "list active":
            await msg.reply("当前账户（Active）：\n" + list_accounts_text("active", chat_id));  return
        if low == "list inactive":
            await msg.reply("当前账户（Inactive）：\n" + list_accounts_text("inactive", chat_id));  return

        if low.startswith("add account"):
            line = text[len("add account"):].strip()
            ok, info = add_account_line(line); await msg.reply(info); return

        if low.startswith("list accounts"):
            arg = text[len("list accounts"):].strip()
            filt = arg if (arg in TIERS or arg.lower() in ("active", "inactive")) else None
            await msg.reply("当前账户：\n" + list_accounts_text(filt, chat_id)); return

        m_tier = re.match(r"^\s*set\s+tier\s+#(\d+)\s+(大额户|中额户|小额户)\s*$", text)
        if m_tier:
            acct_id, tier = m_tier.group(1), m_tier.group(2)
            ok, info = set_account_tier(acct_id, tier); await msg.reply(info); return

        m_status = re.match(r"^\s*set\s+status\s+#(\d+)\s+(Active|Inactive)\s*$", text, re.I)
        if m_status:
            acct_id, st = m_status.group(1), m_status.group(2)
            ok, info = set_account_status(acct_id, st); await msg.reply(info); return

        m_set_amt = re.match(r"^\s*set\s+amount\s+#(\d+)\s+([-+]?\d+(?:\.\d+)?)\s*$", text, re.I)
        if m_set_amt:
            acct_id, val = m_set_amt.group(1), float(m_set_amt.group(2))
            ok, info = set_account_amount(acct_id, val); await msg.reply(info); return

        m_add_amt = re.match(r"^\s*add\s+amount\s+#(\d+)\s+([-+]\d+(?:\.\d+)?)\s*$", text, re.I)
        if m_add_amt:
            acct_id, delta = m_add_amt.group(1), float(m_add_amt.group(2))
            ok, info = add_account_amount(acct_id, delta); await msg.reply(info); return

    # ledger commands
    if low == "bill":
        await msg.reply(format_overview(chat_id));  return

    if low == "bill detail":
        await msg.reply(format_detail(chat_id));  return

    if low.startswith("bill +"):
        if not is_admin:  return
        amt, note = parse_amount_and_note(text[len("bill +"):])
        if amt is None:
            ex = "bill + 50 memo" if get_lang(chat_id) == "en" else "bill + 50 备注"
            await msg.reply(t("err_amount", chat_id, ex=ex));  return
        ok, eid = record_entry(chat_id, "ADJ", abs(amt), user_tag, note)
        await msg.reply(f"✅ +${abs(amt):.2f}（{eid}）\n{format_overview(chat_id)}");  return

    if low.startswith("bill -"):
        if not is_admin:  return
        amt, note = parse_amount_and_note(text[len("bill -"):])
        if amt is None:
            ex = "bill - 30 fee" if get_lang(chat_id) == "en" else "bill - 30 手续费"
            await msg.reply(t("err_amount", chat_id, ex=ex));  return
        ok, eid = record_entry(chat_id, "ADJ", -abs(amt), user_tag, note)
        await msg.reply(f"✅ -${abs(amt):.2f}（{eid}）\n{format_overview(chat_id)}");  return

    if low.startswith("payout"):
        if not is_admin:  return
        amt, note = parse_amount_and_note(text[len("payout"):])
        if amt is None or amt <= 0:
            ex = "payout 900 memo" if get_lang(chat_id) == "en" else "payout 900 备注"
            await msg.reply(t("err_amount", chat_id, ex=ex));  return

        ok, eid = record_entry(chat_id, "PAY", amt, user_tag, note)

        # FIFO effect quick summary (optional: keep simple here)
        overview = format_overview(chat_id)
        await msg.reply(f"✅ Payout ${amt:.2f}（{eid}）\n{overview}")
        return

    # Shout → choose account by inferred amount tier
    if re.search(r"(e转|e\s*转|emt|e转账|e\s*转账|etransfer)", text, re.I):
        amt = extract_amount(text)
        tier = map_amount_to_tier(amt)
        acct = choose_account_by_tier(tier, chat_id)
        if acct:
            await msg.reply(t("reply_account", chat_id, acct=acct))
        else:
            await msg.reply(t("no_account_tier", chat_id, tier=tier))
        return

    # Mention → GPT
    me = await bot.get_me()
    if f"@{me.username}".lower() in low:
        clean = re.sub(fr"@{re.escape(me.username)}", "", text, flags=re.I).strip() or ("你好" if get_lang(chat_id) == "cn" else "Hello")
        ans = await ask_openai(clean, chat_id=chat_id)
        add_memory(chat_id, "user", clean)
        add_memory(chat_id, "assistant", ans)
        await msg.reply(ans[:4000])
        return

    # Ignore others
    return

@router.message(F.chat.type == "private")
async def on_private(msg: types.Message):
    text = (msg.text or "").strip()
    if not text:
        return

    uid = msg.from_user.id
    is_admin = (uid == OWNER_ID) or (uid in ALLOWED_USERS)
    low = text.lower()
    chat_id = msg.chat.id

    # help
    if low == "command":
        await msg.reply(I18N["help"][get_lang(chat_id)]); return

    # language in DM (applies to this DM only)
    if low == "version":
        await msg.reply(t("version_now", chat_id, lang=LANG_LABEL[get_lang(chat_id)])); return
    m_setv = re.match(r"^\s*set\s+version\s+(en|cn)\s*$", low)
    if m_setv:
        if not is_admin:
            await msg.reply(t("not_admin", chat_id)); return
        set_lang(chat_id, m_setv.group(1))
        await msg.reply(t("version_set_ok", chat_id, lang=LANG_LABEL[get_lang(chat_id)])); return

    # broadcast (admin only, from DM)
    if is_admin and low.startswith("broadcast"):
        payload = text[len("broadcast"):].strip()
        if not payload:
            await msg.reply("用法 / Usage：broadcast <message>")
            return
        ok, total = await do_broadcast(payload)
        await msg.reply(t("broadcast_done", chat_id, ok=ok, total=total)); return

    # admin account commands (DM)
    if is_admin:
        if low == "list active":
            await msg.reply("当前账户（Active）：\n" + list_accounts_text("active", chat_id));  return
        if low == "list inactive":
            await msg.reply("当前账户（Inactive）：\n" + list_accounts_text("inactive", chat_id));  return

        if low.startswith("add account"):
            line = text[len("add account"):].strip()
            ok, info = add_account_line(line);  await msg.reply(info);  return

        if low.startswith("list accounts"):
            arg = text[len("list accounts"):].strip()
            filt = arg if (arg in TIERS or arg.lower() in ("active", "inactive")) else None
            await msg.reply("当前账户：\n" + list_accounts_text(filt, chat_id));  return

        m_tier = re.match(r"^\s*set\s+tier\s+#(\d+)\s+(大额户|中额户|小额户)\s*$", text)
        if m_tier:
            acct_id, tier = m_tier.group(1), m_tier.group(2)
            ok, info = set_account_tier(acct_id, tier);  await msg.reply(info);  return

        m_status = re.match(r"^\s*set\s+status\s+#(\d+)\s+(Active|Inactive)\s*$", text, re.I)
        if m_status:
            acct_id, st = m_status.group(1), m_status.group(2)
            ok, info = set_account_status(acct_id, st);  await msg.reply(info);  return

        m_set_amt = re.match(r"^\s*set\s+amount\s+#(\d+)\s+([-+]?\d+(?:\.\d+)?)\s*$", text, re.I)
        if m_set_amt:
            acct_id, val = m_set_amt.group(1), float(m_set_amt.group(2))
            ok, info = set_account_amount(acct_id, val);  await msg.reply(info);  return

        m_add_amt = re.match(r"^\s*add\s+amount\s+#(\d+)\s+([-+]\d+(?:\.\d+)?)\s*$", text, re.I)
        if m_add_amt:
            acct_id, delta = m_add_amt.group(1), float(m_add_amt.group(2))
            ok, info = add_account_amount(acct_id, delta);  await msg.reply(info);  return

    # Otherwise → GPT
    ans = await ask_openai(text, chat_id=chat_id)
    add_memory(chat_id, "user", text)
    add_memory(chat_id, "assistant", ans)
    await msg.reply(ans[:4000])

# =================== Main ===================

async def main():
    log.info("Bot starting…")
    await dp.start_polling(bot, allowed_updates=["message"])

if __name__ == "__main__":
    asyncio.run(main())
