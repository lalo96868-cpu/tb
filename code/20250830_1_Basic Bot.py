# bot.py — Telegram 客服机器人（Aiogram v3 + OpenAI Responses API + 贴卡账户管理）
# 特性：
# - 群聊仅在【喊单关键词】或【@提及机器人】或【白名单管理员命令】时回复；其余消息忽略
# - 金额分档：>=1000→大额户；500<金额<1000→中额户；其它/未识别→小额户
# - 仅从 Active 且匹配档位的账户中【轮询】贴卡（accounts.txt）
# - 账户行新格式：#编号 - 姓名 - 邮箱 - Active|Inactive - 大额户|中额户|小额户 - 已收款金额
#   兼容旧格式（无金额，按0处理），写回时会补齐金额
# - 管理命令（私聊 & 群聊仅限 OWNER/ALLOWED）：
#     add account <完整一行>
#     list accounts [大额户|中额户|小额户|active|inactive]
#     set tier #001 大额户
#     set status #001 Active
#     set amount #001 3500
#     add amount #001 +200 / -150
# - 群里 @机器人 → GPT 回复；私聊非管理消息 → GPT 回复（含最近5条上下文记忆）

import os
import re
import asyncio
import logging
from typing import Optional, List, Dict, Tuple
from collections import deque

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
import httpx

# --------- KEY ----------
TELEGRAM_TOKEN = "8034458767:AAEPeLJlP_XOksWyiWSYFDcpj9SOAQbTN3w"
OPENAI_API_KEY = "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"
OPENAI_MODEL = "gpt-5-mini"
# -------------------------

# --------- 权限 ----------
OWNER_ID = 7681963841
ALLOWED_USERS = {7825042384, 7449394947, 7681963841, 7983854144,8172982320}
# -------------------------

# --------- 文件 ----------
ACCOUNTS_FILE = "accounts.txt"
KB_FILE = "knowledge.txt"  # 预留
# -------------------------

# --------- 短期记忆（GPT） ----------
MEMORY: Dict[int, deque] = {}
MAX_MEMORY = 5
# -------------------------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("support-bot")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# =================== 账户解析/写入（含金额） ===================

# 新格式（6段）
LINE_RE_NEW = re.compile(
    r"^\s*#(?P<id>\d+)\s*-\s*(?P<name>[^-]+?)\s*-\s*(?P<email>[^-]+?)\s*-\s*"
    r"(?P<status>Active|Inactive)\s*-\s*(?P<tier>大额户|中额户|小额户|emt)\s*-\s*(?P<amount>[-+]?\d+(?:\.\d+)?)\s*$"
)
# 旧格式（5段）
LINE_RE_OLD = re.compile(
    r"^\s*#(?P<id>\d+)\s*-\s*(?P<name>[^-]+?)\s*-\s*(?P<email>[^-]+?)\s*-\s*"
    r"(?P<status>Active|Inactive)\s*-\s*(?P<tier>大额户|中额户|小额户|emt)\s*$"
)

TIERS = ("大额户", "中额户", "小额户")

def parse_account_line(line: str) -> Optional[Dict[str, str]]:
    m = LINE_RE_NEW.match(line)
    if m:
        d = m.groupdict()
        return {
            "id": d["id"].strip(),
            "name": d["name"].strip(),
            "email": d["email"].strip(),
            "status": d["status"].strip(),
            "tier": d["tier"].strip(),
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

def load_account_objs() -> List[Dict[str, str]]:
    if not os.path.isfile(ACCOUNTS_FILE):
        open(ACCOUNTS_FILE, "w").close()
    objs: List[Dict[str, str]] = []
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
    if abs(v - int(v)) < 1e-9:
        return str(int(v))
    return f"{v:.2f}"

def write_account_objs(objs: List[Dict[str, str]]):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        for o in objs:
            f.write(
                f"#{o['id']} - {o['name']} - {o['email']} - {o['status']} - {o['tier']} - {fmt_amount(float(o['amount']))}\n"
            )

def add_account_line(line: str) -> Tuple[bool, str]:
    obj = parse_account_line(line)
    if not obj:
        return False, "❌ 格式错误。新格式示例：#001 - Name - email@example.com - Active - 大额户 - 0"
    objs = load_account_objs()
    if any(o["id"] == obj["id"] for o in objs):
        return False, f"❌ 编号已存在：#{obj['id']}"
    objs.append(obj)
    write_account_objs(objs)
    return True, f"✅ 已添加账户：#{obj['id']} - {obj['name']}"

def list_accounts_text(filter_key: Optional[str] = None) -> str:
    objs = load_account_objs()
    if filter_key:
        k = filter_key.strip().lower()
        if k in ("active", "inactive"):
            objs = [o for o in objs if o["status"].lower() == k]
        elif filter_key in TIERS:
            objs = [o for o in objs if o["tier"] == filter_key]
    return "\n".join(
        f"#{o['id']} - {o['name']} - {o['email']} - {o['status']} - {o['tier']} - {fmt_amount(float(o['amount']))}"
        for o in objs
    ) or "（暂无账户）"

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

# 档位轮询指针
RR_INDEX_BY_TIER: Dict[str, int] = {"大额户": 0, "中额户": 0, "小额户": 0}

def choose_account_by_tier(tier: str) -> Optional[str]:
    objs = [o for o in load_account_objs() if o["status"] == "Active" and o["tier"] == tier]
    if not objs:
        return None
    i = RR_INDEX_BY_TIER.get(tier, 0) % len(objs)
    RR_INDEX_BY_TIER[tier] = (i + 1) % len(objs)
    o = objs[i]
    return f"#{o['id']} - {o['name']} - {o['email']} - {o['status']} - {o['tier']} - {fmt_amount(float(o['amount']))}"

# =================== 短期记忆（GPT） ===================

def add_memory(chat_id: int, role: str, content: str):
    if chat_id not in MEMORY:
        MEMORY[chat_id] = deque(maxlen=MAX_MEMORY)
    MEMORY[chat_id].append({"role": role, "content": content})

def get_memory(chat_id: int):
    return list(MEMORY.get(chat_id, []))

# =================== OpenAI 调用 ===================

async def ask_openai(question: str, *, chat_id: int) -> str:
    system_prompt = (
        "你是‘枫枫’，一个友善可爱的客服。回答简洁自然。"
        "遇到账户管理命令（如 set tier / set status / set amount / add amount / add account / list accounts）不要自行回答，由代码处理。"
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

# =================== 金额提取 + 档位映射 ===================

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
    # 修复边界：>=1000 大额；500<金额<1000 中额；其余 小额
    if amt is None:
        return "小额户"
    if amt >= 1000:
        return "大额户"
    if 500 <= amt < 1000:
        return "中额户"
    return "小额户"

# =================== Handlers ===================

@router.message(Command("start"))
async def on_start(msg: types.Message):
    await msg.reply(
        "你好，我是‘枫枫’💖\n"
        "群里喊单示例：e转 100 / emt800 / e 转 1,200（按金额自动贴 大/中/小额户，仅贴 Active 账户）\n"
        "你也可以 @我 与我对话。\n"
        "管理员命令（私聊或群聊，仅白名单）：\n"
        "  add account <#编号 - 姓名 - 邮箱 - Active|Inactive - 大额户|中额户|小额户 - 金额>\n"
        "  list accounts [大额户|中额户|小额户|active|inactive]\n"
        "  set tier #001 大额户\n"
        "  set status #001 Active\n"
        "  set amount #001 3500\n"
        "  add amount #001 +200"
    )

# --- 群聊：命令（白名单） + 关键词贴卡 + @提及 ---
@router.message(F.chat.type.in_({"group","supergroup"}))
async def on_group(msg: types.Message):
    text = (msg.text or "").strip()
    if not text:
        return

    uid = msg.from_user.id
    is_admin = (uid == OWNER_ID) or (uid in ALLOWED_USERS)

    # 0) 群聊管理员命令（仅白名单）
    if is_admin:
        low = text.lower()

        if low.startswith("add account"):
            line = text[len("add account"):].strip()
            ok, info = add_account_line(line)
            await msg.reply(info)
            return

        if low.startswith("list accounts"):
            arg = text[len("list accounts"):].strip()
            filt = arg if (arg in TIERS or arg.lower() in ("active", "inactive")) else None
            await msg.reply("当前账户：\n" + list_accounts_text(filt))
            return

        m_tier = re.match(r"^\s*set\s+tier\s+#(\d+)\s+(大额户|中额户|小额户)\s*$", text)
        if m_tier:
            acct_id, tier = m_tier.group(1), m_tier.group(2)
            ok, info = set_account_tier(acct_id, tier)
            await msg.reply(info)
            return

        m_status = re.match(r"^\s*set\s+status\s+#(\d+)\s+(Active|Inactive)\s*$", text, re.I)
        if m_status:
            acct_id, st = m_status.group(1), m_status.group(2)
            ok, info = set_account_status(acct_id, st)
            await msg.reply(info)
            return

        m_set_amt = re.match(r"^\s*set\s+amount\s+#(\d+)\s+([-+]?\d+(?:\.\d+)?)\s*$", text, re.I)
        if m_set_amt:
            acct_id, val = m_set_amt.group(1), float(m_set_amt.group(2))
            ok, info = set_account_amount(acct_id, val)
            await msg.reply(info)
            return

        m_add_amt = re.match(r"^\s*add\s+amount\s+#(\d+)\s+([-+]\d+(?:\.\d+)?)\s*$", text, re.I)
        if m_add_amt:
            acct_id, delta = m_add_amt.group(1), float(m_add_amt.group(2))
            ok, info = add_account_amount(acct_id, delta)
            await msg.reply(info)
            return

    # 1) 喊单关键字 → 尝试贴卡（金额→档位）
    if re.search(r"(e转|e\s*转|emt|e转账|e\s*转账|etransfer)", text, re.I):
        amt = extract_amount(text)
        tier = map_amount_to_tier(amt)
        acct = choose_account_by_tier(tier)
        if acct:
            await msg.reply(acct)
        else:
            await msg.reply(f"❌ 暂无可用 {tier}（Active）账户，请联系管理员。")
        return

    # 2) @提及 → GPT
    me = await bot.get_me()
    if f"@{me.username}".lower() in text.lower():
        clean = re.sub(fr"@{re.escape(me.username)}", "", text, flags=re.I).strip()
        if not clean:
            clean = "你好"
        ans = await ask_openai(clean, chat_id=msg.chat.id)
        add_memory(msg.chat.id, "user", clean)
        add_memory(msg.chat.id, "assistant", ans)
        await msg.reply(ans[:4000])
        return

    # 3) 其它消息忽略
    return

# --- 私聊：管理命令 + GPT ---
@router.message(F.chat.type == "private")
async def on_private(msg: types.Message):
    text = (msg.text or "").strip()
    if not text:
        return

    uid = msg.from_user.id
    is_admin = (uid == OWNER_ID) or (uid in ALLOWED_USERS)

    if is_admin:
        low = text.lower()

        if low.startswith("add account"):
            line = text[len("add account"):].strip()
            ok, info = add_account_line(line)
            await msg.reply(info)
            return

        if low.startswith("list accounts"):
            arg = text[len("list accounts"):].strip()
            filt = arg if (arg in TIERS or arg.lower() in ("active", "inactive")) else None
            await msg.reply("当前账户：\n" + list_accounts_text(filt))
            return

        m_tier = re.match(r"^\s*set\s+tier\s+#(\d+)\s+(大额户|中额户|小额户)\s*$", text)
        if m_tier:
            acct_id, tier = m_tier.group(1), m_tier.group(2)
            ok, info = set_account_tier(acct_id, tier)
            await msg.reply(info)
            return

        m_status = re.match(r"^\s*set\s+status\s+#(\d+)\s+(Active|Inactive)\s*$", text, re.I)
        if m_status:
            acct_id, st = m_status.group(1), m_status.group(2)
            ok, info = set_account_status(acct_id, st)
            await msg.reply(info)
            return

        m_set_amt = re.match(r"^\s*set\s+amount\s+#(\d+)\s+([-+]?\d+(?:\.\d+)?)\s*$", text, re.I)
        if m_set_amt:
            acct_id, val = m_set_amt.group(1), float(m_set_amt.group(2))
            ok, info = set_account_amount(acct_id, val)
            await msg.reply(info)
            return

        m_add_amt = re.match(r"^\s*add\s+amount\s+#(\d+)\s+([-+]\d+(?:\.\d+)?)\s*$", text, re.I)
        if m_add_amt:
            acct_id, delta = m_add_amt.group(1), float(m_add_amt.group(2))
            ok, info = add_account_amount(acct_id, delta)
            await msg.reply(info)
            return

    # 非管理命令 → GPT 问答
    ans = await ask_openai(text, chat_id=msg.chat.id)
    add_memory(msg.chat.id, "user", text)
    add_memory(msg.chat.id, "assistant", ans)
    await msg.reply(ans[:4000])

# =================== Main ===================

async def main():
    log.info("Bot starting…")
    await dp.start_polling(bot, allowed_updates=["message"])

if __name__ == "__main__":
    asyncio.run(main())
