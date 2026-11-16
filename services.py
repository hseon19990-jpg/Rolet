"""منطق البوت الأساسي (بدون اعتماد مباشر على aiogram حتى يسهل اختباره)."""
import asyncio
import asyncio as _aio   # نسخة ثابتة (الاختبارات تستبدل asyncio أعلاه)
import hashlib
import logging
import random
import secrets
import string
import time
from html import escape as esc

import ctx
from config import OWNER_IDS

log = logging.getLogger("bot")


def now():
    return int(time.time())


def rand_code(n=8):
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def fmt_time(ts):
    if ts - time.time() > 86400 * 365 * 5:
        return "♾ مفتوحة (ينهيها المنظّم)"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def mention(u):
    name = esc(u.get("first_name") or u.get("username") or str(u["user_id"]))
    return f'<a href="tg://user?id={u["user_id"]}">{name}</a>'


async def audit(db, actor, action, detail=""):
    await db.run("INSERT INTO audit(actor_id,action,detail,created_at) VALUES(?,?,?,?)",
                 actor, action, str(detail), now())


async def safe_send(bot, uid, text, **kw):
    try:
        await bot.send_message(uid, text, **kw)
        return True
    except Exception as e:
        log.info("send to %s failed: %s", uid, e)
        return False


# ───────────── روابط الدعوة والإحالات ─────────────
async def ensure_default_link(db, owner_id):
    await db.run("INSERT OR IGNORE INTO invite_links(code,owner_id,title,is_default,created_at) VALUES(?,?,?,1,?)",
                 rand_code(), owner_id, "default", now())
    return await db.val("SELECT code FROM invite_links WHERE owner_id=? AND is_default=1", owner_id)


async def ensure_channel_link(db, owner_id, chat_id, title):
    await db.run("INSERT OR IGNORE INTO invite_links(code,owner_id,channel_id,title,created_at) VALUES(?,?,?,?,?)",
                 rand_code(), owner_id, chat_id, title, now())
    return await db.val("SELECT code FROM invite_links WHERE owner_id=? AND channel_id=?", owner_id, chat_id)


async def resolve_source(db, payload):
    """يحدد صاحب الرابط من رابط البداية: (owner_id, link_code, source, source_id) أو None."""
    try:
        if payload.startswith("ref_"):
            row = await db.one("SELECT code, owner_id FROM invite_links WHERE code=? AND active=1", payload[4:])
            if row:
                return row["owner_id"], row["code"], "link", None
        elif payload.startswith("ev_") or payload.startswith("v_"):
            eid = int(payload.split("_")[1])
            ev = await db.one("SELECT id, owner_id, channel_id FROM events WHERE id=?", eid)
            if ev:
                code = None
                if ev["channel_id"]:
                    code = await db.val("SELECT code FROM invite_links WHERE owner_id=? AND channel_id=?",
                                        ev["owner_id"], ev["channel_id"])
                if not code:
                    code = await ensure_default_link(db, ev["owner_id"])
                return ev["owner_id"], code, "event", eid
    except (ValueError, IndexError):
        pass
    return None


async def record_referral(db, user_id, src):
    """يسجل الإحالة مرة وحدة فقط لكل مستخدم (user_id مفتاح أساسي) ويمنع الإحالة الذاتية."""
    owner, code, source, sid = src
    if owner == user_id:
        return False
    n = await db.run(
        "INSERT OR IGNORE INTO referrals(user_id,owner_id,link_code,source,source_id,created_at) VALUES(?,?,?,?,?,?)",
        user_id, owner, code, source, sid, now())
    return n == 1


async def validate_referrals(db, bot=None):
    """pending → valid بعد: التحقق + مرور أيام الانتظار + عدم حظر البوت + بقائه بالقنوات الإجبارية العامة.
    من يغادر قناتك قبل الاعتماد يُلغى (يمنع الرشق)."""
    cutoff = now() - int(float(db.get("hold_days")) * 86400)
    await db.run("UPDATE referrals SET status='invalid' WHERE status='pending' AND user_id IN "
                 "(SELECT user_id FROM users WHERE banned=1 OR blocked_bot=1)")
    need_captcha = db.get("captcha_on") == "1"   # التحقق صار يُطلب لكل فعالية، فلا نُسقط الإحالات بسببه إلا مع الكابتشا العامة
    if need_captcha:
        await db.run("UPDATE referrals SET status='invalid' WHERE status='pending' AND created_at<=? AND user_id IN "
                     "(SELECT user_id FROM users WHERE verified=0)", cutoff)
    ready = [r["user_id"] for r in await db.all(
        "SELECT r.user_id FROM referrals r JOIN users u ON u.user_id=r.user_id WHERE r.status='pending' "
        "AND r.created_at<=? AND (u.verified=1 OR ?=0) AND u.banned=0 AND u.blocked_bot=0 ORDER BY r.created_at LIMIT 300",
        cutoff, 1 if need_captcha else 0)]
    if not ready:
        return 0
    chans = await global_mandatory(db) if bot is not None else []
    good, bad = [], []
    for uid in ready:
        left = False
        for ch in chans:
            if await is_member(bot, ch["chat_id"], uid) is False:
                left = True
                break
        if chans:
            await asyncio.sleep(0.04)
        (bad if left else good).append(uid)

    def fn(c):
        c.executemany("UPDATE referrals SET status='valid' WHERE user_id=? AND status='pending'", [(u,) for u in good])
        c.executemany("UPDATE referrals SET status='invalid' WHERE user_id=? AND status='pending'", [(u,) for u in bad])

    await db.tx(fn)
    return len(good)


async def owner_links(db, owner_id):
    rows = await db.all(
        """SELECT l.*, c.title AS ch_title,
        (SELECT COUNT(*) FROM referrals r WHERE r.link_code=l.code AND r.status='valid' AND r.claim_id IS NULL) AS valid,
        (SELECT COUNT(*) FROM referrals r WHERE r.link_code=l.code AND r.status='pending') AS pending,
        (SELECT COUNT(*) FROM referrals r WHERE r.link_code=l.code AND r.claim_id IS NOT NULL) AS claimed
        FROM invite_links l LEFT JOIN channels c ON c.chat_id=l.channel_id
        WHERE l.owner_id=? AND l.active=1 ORDER BY l.is_default DESC, l.created_at""", owner_id)
    for r in rows:
        r["thr"] = r["threshold"] or int(db.get("ref_threshold"))
        r["prc"] = r["price"] if r["price"] is not None else float(db.get("ref_reward"))
    return rows


async def create_payout(db, owner_id):
    """يحجز أعضاء معتمدين (كل threshold عضو = وحدة بسعر الرابط) ويفتح طلب صرف.
    يرجع dict أو "exists" أو None إذا ما في شي مستحق."""
    def fn(c):
        if c.execute("SELECT 1 FROM payouts WHERE owner_id=? AND status='pending'", (owner_id,)).fetchone():
            return "exists"
        links = [dict(r) for r in c.execute("SELECT * FROM invite_links WHERE owner_id=? AND active=1", (owner_id,))]
        pid = c.execute("INSERT INTO payouts(owner_id,amount,count,created_at) VALUES(?,0,0,?)",
                        (owner_id, now())).lastrowid
        total, cnt = 0.0, 0
        for l in links:
            thr = l["threshold"] or int(db.get("ref_threshold"))
            price = l["price"] if l["price"] is not None else float(db.get("ref_reward"))
            rows = c.execute("SELECT user_id FROM referrals WHERE link_code=? AND status='valid' AND claim_id IS NULL "
                             "ORDER BY created_at, user_id", (l["code"],)).fetchall()
            units = len(rows) // thr
            if units <= 0:
                continue
            take = rows[:units * thr]
            c.executemany("UPDATE referrals SET claim_id=? WHERE user_id=?", [(pid, r["user_id"]) for r in take])
            c.execute("UPDATE invite_links SET notified90=0, notified100=0 WHERE code=?", (l["code"],))
            total += units * price
            cnt += units * thr
        if cnt == 0:
            c.execute("DELETE FROM payouts WHERE id=?", (pid,))
            return None
        c.execute("UPDATE payouts SET amount=?, count=? WHERE id=?", (total, cnt, pid))
        return {"id": pid, "amount": total, "count": cnt}

    return await db.tx(fn)


async def decide_payout(db, pid, action, actor):
    p = await db.one("SELECT * FROM payouts WHERE id=?", pid)
    if not p:
        return None
    rules = {"approve": ("pending", "approved"), "reject": ("pending", "rejected"), "paid": ("approved", "paid")}
    need, new = rules[action]
    if p["status"] != need:
        return "bad_state"

    def fn(c):
        c.execute("UPDATE payouts SET status=?, decided_at=?, decided_by=? WHERE id=?", (new, now(), actor, pid))
        if action == "reject":
            c.execute("UPDATE referrals SET claim_id=NULL WHERE claim_id=?", (pid,))

    await db.tx(fn)
    await audit(db, actor, f"payout_{action}", pid)
    return await db.one("SELECT * FROM payouts WHERE id=?", pid)


async def top_channels(db, since=0, limit=10):
    return await db.all(
        """SELECT l.channel_id, c.title, c.username,
        SUM(CASE WHEN r.status IN ('pending','valid') THEN 1 ELSE 0 END) AS brought,
        SUM(CASE WHEN r.status='valid' THEN 1 ELSE 0 END) AS valid
        FROM referrals r JOIN invite_links l ON l.code=r.link_code
        JOIN channels c ON c.chat_id=l.channel_id
        WHERE r.created_at>=? AND c.active=1 GROUP BY l.channel_id
        HAVING brought>0 ORDER BY brought DESC LIMIT ?""", since, limit)


# ───────────── شروط المشاركة ─────────────
async def is_member(bot, chat_id, user_id):
    """True/False، أو None إذا تعذر الفحص (البوت مو مشرف مثلاً)."""
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        st = m.status
        return st in ("member", "administrator", "creator") or (st == "restricted" and bool(getattr(m, "is_member", False)))
    except Exception as e:
        log.warning("cannot check chat %s: %s", chat_id, e)
        return None


async def global_mandatory(db):
    return await db.all("SELECT chat_id,title,username,url FROM mandatory_channels")


GATE_CACHE = {}


def clear_gate(uid):
    GATE_CACHE.pop(uid, None)


async def gate_missing(bot, db, uid):
    """القنوات الإجبارية العامة التي لم يشترك بها المستخدم بعد (فارغة = مسموح)."""
    if db.get("gate_on") != "1":
        return []
    if GATE_CACHE.get(uid, 0) > now():
        return []
    missing = [ch for ch in await global_mandatory(db) if await is_member(bot, ch["chat_id"], uid) is False]
    if not missing:
        GATE_CACHE[uid] = now() + 120
    return missing


async def mandatory_list(db, ev=None):
    rows = await db.all("SELECT chat_id,title,username,url FROM mandatory_channels")
    if ev:
        rows += await db.all("SELECT chat_id,title,username,url FROM event_channels WHERE event_id=?", ev["id"])
    return rows


async def check_user(bot, db, ev, user_id):
    """يرجع قائمة الشروط الناقصة [(code, data)]؛ فارغة = مؤهل.
    المالك والمشرفون: يتجاوزون الكابتشا واليوزر والصورة تلقائياً،
    وشروط بريميوم/تعليق يعتمدونها يدوياً بزر (جدول admin_overrides)."""
    is_adm = ctx.is_admin(user_id)
    u = await db.one("SELECT * FROM users WHERE user_id=?", user_id)
    if not u or u["banned"]:
        return [("banned", None)]

    async def approved(code):
        return is_adm and await db.val(
            "SELECT 1 FROM admin_overrides WHERE event_id=? AND user_id=? AND code=?", ev["id"], user_id, code)

    fails = []
    # التحقق (كابتشا) اختياري لكل فعالية: يقرره منظّمها (events.verify). إن طُلب فهو على الجميع بمن فيهم المشرفون
    if ev.get("verify", 1) and not u["verified"]:
        fails.append(("captcha", None))
    for ch in await mandatory_list(db, ev):
        if await is_member(bot, ch["chat_id"], user_id) is False:
            fails.append(("channel", ch))
    if ev["need_premium"] and not u["is_premium"] and not await approved("premium"):
        fails.append(("premium", None))
    if (ev["comment_chat"] or ev["comment_url"]) and not await db.val(
            "SELECT 1 FROM comment_hits WHERE event_id=? AND user_id=?", ev["id"], user_id) \
            and not await approved("comment"):
        fails.append(("comment", ev["comment_url"]))
    if ev["need_bot_url"] and not await db.val(
            "SELECT 1 FROM confirms WHERE event_id=? AND user_id=?", ev["id"], user_id):
        fails.append(("botconfirm", ev["need_bot_url"]))
    if ev["vote_url"] and not await db.val(
            "SELECT 1 FROM vote_confirms WHERE event_id=? AND user_id=?", ev["id"], user_id):
        fails.append(("voteconfirm", ev["vote_url"]))
    if db.get("require_username") == "1" and not u["username"] and not is_adm:
        fails.append(("username", None))
    if db.get("require_photo") == "1" and not is_adm:
        try:
            photos = await bot.get_user_profile_photos(user_id, limit=1)
            if photos.total_count == 0:
                fails.append(("photo", None))
        except Exception:
            pass
    return fails


KIND_LABEL = {"roulette": "🎡 روليت", "votes": "🏆 مسابقة أصوات", "chroulette": "🌐 رولت القنوات"}
KIND_ICON = {"roulette": "🎡", "votes": "🏆", "chroulette": "🌐"}


async def event_text(db, ev):
    lines = [f"{KIND_LABEL.get(ev['kind'], '')}\n\n<b>{esc(ev['title'])}</b>"]
    if ev["prize"]:
        lines.append(f"🎁 الجائزة: {esc(ev['prize'])}")
    lines.append(f"👑 عدد الفائزين: {ev['winners']}")
    lines.append(f"⏰ تنتهي: {fmt_time(ev['end_at'])}")
    if ev["kind"] != "votes" and ev["join_goal"]:
        lines.append(f"👥 تنتهي الفعالية عند وصول المنضمين إلى {ev['join_goal']}")
    if ev["kind"] == "votes" and ev["vote_goal"]:
        lines.append(f"🗳 تنتهي المسابقة عند وصول أحد المتسابقين إلى {ev['vote_goal']} صوت")
    if ev["kind"] == "chroulette":
        lines.append("\n🌐 <b>إمكانية مشاركة مشرفين ومالك القناة</b>")
    elif ev["invite_bonus"]:
        lines.append(f"🎟 ادعُ أصدقاءك: كل صديق = فرصة إضافية (حتى {ev['invite_bonus']})")
    conds = []
    if ev["kind"] == "chroulette":
        conds += ["• يجب رفع البوت مشرفاً في قناتك", "• يجب فتح صلاحية الإرسال للبوت"]
    conds += [f"• الاشتراك بـ {esc(ch['title'] or '')}" for ch in await mandatory_list(db, ev)]
    if ev["need_premium"]:
        conds.append("• حساب تليجرام بريميوم")
    if ev["comment_chat"] or ev["comment_url"]:
        here = f'<a href="{ev["comment_url"]}">علق هنا</a>' if ev["comment_url"] else "علق هنا"
        conds.append(f"• 💬 التعليق: {here}")
    if ev["need_bot_url"]:
        conds.append("• تفعيل بوت معيّن")
    if ev["vote_url"]:
        conds.append(f'• <a href="{ev["vote_url"]}">التصويت عبر رابط التعليق</a>')
    if ev.get("verify", 1):
        conds.append("• 🔐 اجتياز التحقق (كابتشا)")
    if conds:
        lines.append("\n📌 الشروط:\n" + "\n".join(conds))
    return "\n".join(lines)


# ───────────── منشور القناة: أسماء المتسابقين + تحديث ─────────────
_post_last = {}


def is_manual(ev):
    """التحديد اليدوي للفائز متاح فقط لفعاليات مالك البوت الأصلي. غير ذلك: سحب عشوائي دائماً."""
    return ev["draw_mode"] == "manual" and ev["owner_id"] in OWNER_IDS


async def joined_count(db, ev):
    if ev["kind"] == "votes":
        return await db.val("SELECT COUNT(*) FROM candidates WHERE approved=1 AND event_id=?", ev["id"]) or 0
    return await db.val("SELECT COUNT(*) FROM participants WHERE event_id=?", ev["id"]) or 0


async def post_text(db, ev):
    text = await event_text(db, ev)
    n = await joined_count(db, ev)
    text += f"\n\n👥 <b>{'المتسابقون' if ev['kind'] == 'votes' else 'المنضمون'}: {n}</b>"
    if ev["kind"] == "votes":
        rows = await db.all(
            "SELECT c.name, COUNT(v.voter_id) AS n FROM candidates c LEFT JOIN votes v ON v.candidate_id=c.id "
            "WHERE c.approved=1 AND c.event_id=? GROUP BY c.id ORDER BY n DESC, c.created_at LIMIT 25", ev["id"])
        if rows:
            text += "\n\n🙋 <b>المتسابقون:</b>\n" + "\n".join(
                f"{i}. {esc(r['name'])} — {r['n']} صوت" for i, r in enumerate(rows, 1))
    return text


def post_markup(ev):
    import ctx
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    link = f"https://t.me/{ctx.bot_username}?start=ev_{ev['id']}"
    kb = InlineKeyboardBuilder()
    if ev["kind"] == "roulette":   # واجهة القناة: انضم | انسحب، شارك (إن فُعّلت الدعوات)، وزر المسؤول
        kb.button(text="✅ انضم", callback_data=f"chj:{ev['id']}")
        kb.button(text="🚪 انسحب", callback_data=f"chl:{ev['id']}")
        if ev["invite_bonus"]:
            kb.button(text="📤 شارك", callback_data=f"chs:{ev['id']}")
        kb.button(text="👑 للمسؤول: اسحب فائز", callback_data=f"chwin:{ev['id']}")
        kb.adjust(2, 1, 1) if ev["invite_bonus"] else kb.adjust(2, 1)
        return kb.as_markup()
    if ev["kind"] != "votes":
        kb.button(text="🎡 شارك الآن", url=link)
    else:
        kb.button(text="🙋 سجّل كمتسابق", url=link)
        kb.button(text="🗳 صوّت", url=link)
        kb.button(text="📊 الترتيب", callback_data=f"rank:{ev['id']}")
    kb.button(text="👑 للمسؤول: اسحب فائز", callback_data=f"chwin:{ev['id']}")
    kb.adjust(1)
    return kb.as_markup()


async def refresh_post(bot, db, ev_id, force=False):
    """يحدّث منشور القناة (أسماء المتسابقين/الأصوات) مع حدّ زمني ضد الإغراق."""
    ev = await db.one("SELECT * FROM events WHERE id=?", ev_id)
    if not ev or ev["status"] != "active" or not ev["channel_id"] or not ev["message_id"]:
        return
    if not force and time.time() - _post_last.get(ev_id, 0) < 45:
        return
    _post_last[ev_id] = time.time()
    for attempt in range(3):
        try:
            await bot.edit_message_text(await post_text(db, ev), chat_id=ev["channel_id"],
                                        message_id=ev["message_id"], reply_markup=post_markup(ev))
            return
        except Exception as e:
            wait = getattr(e, "retry_after", None)
            if wait and attempt < 2:   # حد تليجرام للتعديل: ننتظر ونعيد المحاولة
                await _aio.sleep(min(int(wait) + 1, 30))
                continue
            if "not modified" not in str(e):
                log.info("refresh_post failed: %s", e)
            return


async def check_join_goal(bot, db, ev_id):
    """عند بلوغ عدد المنضمين المطلوب تنتهي الفعالية (أو ينبَّه مالك البوت إن كانت يدوية)."""
    ev = await db.one("SELECT * FROM events WHERE id=?", ev_id)
    if not ev or ev["status"] != "active" or ev["kind"] == "votes" or not ev["join_goal"]:
        return
    n = await db.val("SELECT COUNT(*) FROM participants WHERE event_id=?", ev_id)
    if n < ev["join_goal"]:
        return
    if is_manual(ev):
        if await db.run("UPDATE events SET draw_notified=1 WHERE id=? AND draw_notified=0", ev_id):
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            kb = InlineKeyboardBuilder()
            kb.button(text="🎯 اختيار الفائز بنفسي", callback_data=f"evpick:{ev_id}")
            kb.button(text="🎡 تدوير العجلة (عشوائي)", callback_data=f"evend:{ev_id}")
            kb.adjust(1)
            await safe_send(bot, ev["owner_id"],
                            f"👥 وصل المنضمون إلى {ev['join_goal']} في «{esc(ev['title'])}» (#{ev_id}).\nاختر طريقة تحديد الفائز:",
                            reply_markup=kb.as_markup())
        return
    await finish_event(bot, db, ev_id)


_pending_refresh = {}


def schedule_refresh(bot, db, ev_id, delay=1.0):
    """تحديث فوري تقريباً لمنشور القناة عند انضمام/مغادرة أحد.
    يجمع التغييرات المتقاربة بتحديث واحد (ثانية) لتفادي حدود تليجرام، والعدد يكون دائماً الأحدث."""
    if ev_id in _pending_refresh:
        return

    async def _run():
        try:
            await _aio.sleep(delay)
        finally:
            _pending_refresh.pop(ev_id, None)
        try:
            await refresh_post(bot, db, ev_id, force=True)
        except Exception:
            log.exception("schedule_refresh failed")

    _pending_refresh[ev_id] = _aio.create_task(_run())


# ───────────── إنهاء الفعاليات والسحب ─────────────
async def user_row(db, uid):
    return await db.one("SELECT user_id,username,first_name FROM users WHERE user_id=?", uid) or \
        {"user_id": uid, "username": None, "first_name": str(uid)}


def draw_order(ids, seed, tickets=None):
    """ترتيب عشوائي قابل للتحقق. tickets = {مستخدم: عدد الفرص}: كل فرصة تزيد احتمال فوزه."""
    tickets = tickets or {}
    pool = []
    for u in sorted(ids):
        pool += [u] * max(1, int(tickets.get(u, 1)))
    random.Random(seed).shuffle(pool)
    seen, order = set(), []
    for u in pool:
        if u not in seen:
            seen.add(u)
            order.append(u)
    return order


async def has_valid_entry(bot, db, ev, uid):
    """رولت القنوات: يبقى مؤهلاً فقط إذا ما زال مشرفاً بقناة واحدة على الأقل من قنواته المسجلة."""
    for e in await db.all("SELECT chat_id FROM event_entries WHERE event_id=? AND user_id=?", ev["id"], uid):
        try:
            m = await bot.get_chat_member(e["chat_id"], uid)
            if m.status in ("administrator", "creator"):
                return True
        except Exception:
            pass
    return False


async def finish_event(bot, db, event_id, manual=None):
    """manual = قائمة ايديات فائزين يحددها المنظّم (بدل السحب التلقائي)."""
    if not await db.run("UPDATE events SET status='ending' WHERE id=? AND status='active'", event_id):
        return None
    ev = await db.one("SELECT * FROM events WHERE id=?", event_id)
    winners, proof, ranking = [], None, []
    if manual:
        winners = list(dict.fromkeys(manual))[:ev["winners"]]
        proof = "manual"
        if ev["kind"] == "votes":
            ranking = await db.all(
                "SELECT c.user_id, c.name, COUNT(v.voter_id) AS votes FROM candidates c "
                "LEFT JOIN votes v ON v.candidate_id=c.id WHERE c.approved=1 AND c.event_id=? GROUP BY c.id "
                "ORDER BY votes DESC, c.created_at", event_id)
    elif ev["kind"] in ("roulette", "chroulette"):
        tickets = {}
        if ev["kind"] == "chroulette":
            ids = [r["user_id"] for r in await db.all("SELECT DISTINCT user_id FROM event_entries WHERE event_id=?", event_id)]
            for r in await db.all("SELECT user_id, COUNT(*) AS n FROM event_entries WHERE event_id=? GROUP BY user_id", event_id):
                tickets[r["user_id"]] = min(r["n"], ev["max_channels"])
        else:
            ids = [r["user_id"] for r in await db.all("SELECT user_id FROM participants WHERE event_id=?", event_id)]
            if ev["invite_bonus"]:
                for r in await db.all(
                        "SELECT i.inviter_id AS u, COUNT(*) AS n FROM event_invites i JOIN participants p "
                        "ON p.event_id=i.event_id AND p.user_id=i.user_id WHERE i.event_id=? GROUP BY i.inviter_id", event_id):
                    tickets[r["u"]] = 1 + min(r["n"], ev["invite_bonus"])
        seed = secrets.token_hex(8)
        digest = hashlib.sha256(",".join(f"{u}:{tickets.get(u, 1)}" for u in sorted(ids)).encode()).hexdigest()[:16]
        for uid in draw_order(ids, seed, tickets):
            if len(winners) >= ev["winners"]:
                break
            # نعيد فحص الشروط لحظة السحب؛ الفائز المخالف يُستبدل تلقائياً
            ok = not await check_user(bot, db, ev, uid)
            if ok and ev["kind"] == "chroulette":
                ok = await has_valid_entry(bot, db, ev, uid)
            if ok:
                winners.append(uid)
            await asyncio.sleep(0.05)
        proof = f"{len(ids)}:{digest}:{seed}"
    else:
        ranking = await db.all(
            "SELECT c.user_id, c.name, COUNT(v.voter_id) AS votes FROM candidates c "
            "LEFT JOIN votes v ON v.candidate_id=c.id WHERE c.approved=1 AND c.event_id=? GROUP BY c.id "
            "ORDER BY votes DESC, c.created_at", event_id)
        for r in ranking:
            if len(winners) >= ev["winners"]:
                break
            if r["votes"] > 0 and not await check_user(bot, db, ev, r["user_id"]):
                winners.append(r["user_id"])
    for i, uid in enumerate(winners, 1):
        await db.run("INSERT OR IGNORE INTO winners(event_id,user_id,place) VALUES(?,?,?)", event_id, uid, i)
    await db.run("UPDATE events SET status='ended', proof=? WHERE id=?", proof, event_id)
    await announce(bot, db, ev, winners, proof, ranking)
    await audit(db, 0, "event_finished", event_id)
    return winners


async def wheel_animation(bot, chat_id, names):
    try:
        msg = await bot.send_message(chat_id, "🎡 <b>العجلة تدور...</b>")
    except Exception:
        return None
    frames = ["🎡", "🎰", "🎡", "🎰"]
    for i in range(7):
        pick = random.choice(names) if names else "..."
        try:
            await bot.edit_message_text(f"{frames[i % 4]} <b>العجلة تدور...</b>\n\n➡️ {esc(pick)}",
                                        chat_id=chat_id, message_id=msg.message_id)
        except Exception:
            pass
        await asyncio.sleep(1.2)
    return msg


async def announce(bot, db, ev, winners, proof, ranking):
    wrows = [await user_row(db, w) for w in winners]
    lines = [f"🏁 <b>انتهت الفعالية</b>\n\n<b>{esc(ev['title'])}</b>"]
    if ev["kind"] == "votes" and ranking:
        lines.append("\n📊 الترتيب:")
        for i, r in enumerate(ranking[:10], 1):
            lines.append(f"{i}. {esc(r['name'])} — {r['votes']} صوت")
    if wrows:
        lines.append("\n🏆 الفائزون:")
        lines += [f"{i}. {mention(w)}" for i, w in enumerate(wrows, 1)]
    else:
        lines.append("\nلا يوجد فائزون مؤهلون.")
    text = "\n".join(lines)
    if ev["channel_id"]:
        names = []
        if ev["kind"] != "votes":
            rows = await db.all("SELECT u.first_name, u.username FROM participants p JOIN users u ON u.user_id=p.user_id "
                                "WHERE p.event_id=? ORDER BY RANDOM() LIMIT 30", ev["id"])
            names = [r["first_name"] or r["username"] or "?" for r in rows]
            msg = await wheel_animation(bot, ev["channel_id"], names)
            try:
                if msg:
                    await bot.edit_message_text(text, chat_id=ev["channel_id"], message_id=msg.message_id)
                else:
                    await bot.send_message(ev["channel_id"], text)
            except Exception as e:
                log.warning("announce failed: %s", e)
        else:
            await safe_send(bot, ev["channel_id"], text)
        if ev["message_id"]:
            try:
                await bot.edit_message_reply_markup(chat_id=ev["channel_id"], message_id=ev["message_id"],
                                                    reply_markup=None)
            except Exception:
                pass
    owner_note = f"✅ انتهت فعاليتك #{ev['id']}\n\n" + text
    await safe_send(bot, ev["owner_id"], owner_note)
    owner = await user_row(db, ev["owner_id"])
    for w in winners:
        prize = f"\n🎁 الجائزة: {esc(ev['prize'])}" if ev["prize"] else ""
        await safe_send(bot, w, f"🎉 مبروك! فزت في «{esc(ev['title'])}»{prize}\n\nتواصل مع المنظّم: {mention(owner)}")


async def check_spike(bot, db, ev, cand_id):
    n = await db.val("SELECT COUNT(*) FROM votes WHERE candidate_id=? AND created_at>=?", cand_id, now() - 60)
    if n >= int(db.get("vote_spike")) and not ev["spike_alerted"]:
        await db.run("UPDATE events SET spike_alerted=1 WHERE id=?", ev["id"])
        name = await db.val("SELECT name FROM candidates WHERE id=?", cand_id)
        for uid in {ev["owner_id"]} | set(OWNER_IDS):
            await safe_send(bot, uid, f"🚨 نشاط تصويت مشبوه في الفعالية #{ev['id']}: "
                                      f"{esc(name or '')} حصل على {n} صوت خلال دقيقة. راجع الأصوات.")


# ───────────── المهام الدورية ─────────────
async def send_reminders(bot, db):
    rows = await db.all("SELECT * FROM events WHERE status='active' AND reminded=0 AND end_at-?<=3600", now())
    for ev in rows:
        await db.run("UPDATE events SET reminded=1 WHERE id=?", ev["id"])
        if ev["kind"] == "votes":
            cnt = await db.val("SELECT COUNT(*) FROM candidates WHERE approved=1 AND event_id=?", ev["id"])
        else:
            cnt = await db.val("SELECT COUNT(*) FROM participants WHERE event_id=?", ev["id"])
        await safe_send(bot, ev["owner_id"], f"⏰ فعاليتك «{esc(ev['title'])}» تنتهي خلال أقل من ساعة. المشاركون: {cnt}")
        if ev["kind"] != "votes":   # تذكير للمشاركين (حتى 300)
            ps = await db.all("SELECT p.user_id FROM participants p JOIN users u ON u.user_id=p.user_id "
                              "WHERE p.event_id=? AND u.blocked_bot=0 AND u.banned=0 LIMIT 300", ev["id"])
            for p in ps:
                await safe_send(bot, p["user_id"], f"⏰ الروليت «{esc(ev['title'])}» ينتهي خلال أقل من ساعة. حظاً موفقاً 🍀")
                await asyncio.sleep(0.05)


async def notify_thresholds(bot, db):
    rows = await db.all("SELECT * FROM invite_links WHERE active=1 AND notified100=0")
    for l in rows:
        thr = l["threshold"] or int(db.get("ref_threshold"))
        valid = await db.val("SELECT COUNT(*) FROM referrals WHERE link_code=? AND status='valid' AND claim_id IS NULL",
                             l["code"])
        if valid >= thr:
            await db.run("UPDATE invite_links SET notified90=1, notified100=1 WHERE code=?", l["code"])
            await safe_send(bot, l["owner_id"], f"🎉 وصلت إلى {thr} عضو معتمد! افتح «روابطي ومكافآتي» واطلب الصرف.")
        elif valid >= 0.9 * thr and not l["notified90"]:
            await db.run("UPDATE invite_links SET notified90=1 WHERE code=?", l["code"])
            await safe_send(bot, l["owner_id"], f"🔥 اقتربت! وصلت إلى {valid} من {thr} عضو معتمد.")


_warned_comment = set()


async def check_channels(bot, db):
    for ch in await db.all("SELECT * FROM channels WHERE active=1"):
        try:
            me = await bot.get_chat_member(ch["chat_id"], bot.id)
            ok = me.status == "administrator"
        except Exception:
            ok = False
        if not ok:
            await db.run("UPDATE channels SET active=0 WHERE chat_id=?", ch["chat_id"])
            await safe_send(bot, ch["owner_id"], f"⚠️ البوت لم يعد مشرفاً في «{esc(ch['title'] or '')}». "
                                                 f"تم إخفاء القناة من الأقسام حتى تعيد إضافتي كمشرف.")
    for ev in await db.all("SELECT id,owner_id,title,comment_chat FROM events "
                           "WHERE status='active' AND comment_chat IS NOT NULL AND COALESCE(comment_text,'')!='confirm'"):
        try:
            me = await bot.get_chat_member(ev["comment_chat"], bot.id)
            ok = me.status == "administrator"
        except Exception:
            ok = False
        if not ok and ev["id"] not in _warned_comment:
            _warned_comment.add(ev["id"])
            await safe_send(bot, ev["owner_id"], f"⚠️ البوت لم يعد مشرفاً بكروب التعليق في فعالية «{esc(ev['title'])}» "
                                                 f"(#{ev['id']}). لن تُحتسب التعليقات حتى تعيده مشرفاً.")
        elif ok:
            _warned_comment.discard(ev["id"])
    for ch in await db.all("SELECT * FROM mandatory_channels"):
        try:
            me = await bot.get_chat_member(ch["chat_id"], bot.id)
            ok = me.status == "administrator"
        except Exception:
            ok = False
        if not ok:
            for uid in OWNER_IDS:
                await safe_send(bot, uid, f"⚠️ القناة الإجبارية «{esc(ch['title'] or '')}» البوت ليس مشرفاً فيها، "
                                          f"لن يعمل فحص الاشتراك لها.")


async def daily_backup(bot, db):
    if db.get("backup_daily") != "1" or not OWNER_IDS:
        return
    if now() - int(db.get("last_backup")) < 86400:
        return
    from aiogram.types import FSInputFile
    path = f"/tmp/backup_{now()}.db"
    await asyncio.to_thread(db.backup_to, path)
    await db.set("last_backup", now())
    try:
        await bot.send_document(min(OWNER_IDS), FSInputFile(path), caption="🗄 نسخة احتياطية يومية")
    except Exception as e:
        log.warning("backup send failed: %s", e)


def open_link(u):
    """اسم الشخص كرابط tg://openmessage?user_id=ID + اليوزر + الايدي."""
    name = esc(u.get("first_name") or u.get("username") or str(u["user_id"]))
    uname = f" • @{esc(u['username'])}" if u.get("username") else ""
    return f'<a href="tg://openmessage?user_id={u["user_id"]}">{name}</a>{uname} • <code>{u["user_id"]}</code>'


async def notify_join(bot, db, ev, uid, extra=""):
    """إشعار لصاحب الروليت عند انضمام أي شخص."""
    if not ev["notify_owner"] or ev["owner_id"] == uid:
        return
    u = await user_row(db, uid)
    n = await db.val("SELECT COUNT(*) FROM participants WHERE event_id=?", ev["id"])
    await safe_send(bot, ev["owner_id"], f"🔔 انضمام جديد للفعالية «{esc(ev['title'])}» (#{ev['id']})\n"
                                         f"👤 {open_link(u)}{extra}\n👥 المشاركون: {n}")


# ───────────── رولت القنوات: مراقبة حذف رسالة الفعالية من قنوات المشاركين ─────────────
REPOST_DELAY = 180      # بعد كم ثانية من الحذف يعيد البوت نشر الرسالة تلقائياً
WATCH_EVERY = 60        # كل كم ثانية يفحص البوت وجود الرسائل
_entry_lock = asyncio.Lock()


def promo_text(ev):
    return esc(ev["promo_text"] or ev["title"])


def promo_markup(ev_id):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="🎡 شارك بالروليت", url=f"https://t.me/{ctx.bot_username}?start=ev_{ev_id}")
    return kb.as_markup()


def return_markup(ev_id, chat_id):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 إعادة الإرسال", callback_data=f"cret:{ev_id}:{chat_id}")
    return kb.as_markup()


async def msg_exists(bot, chat_id, message_id, ev_id):
    """True = الرسالة موجودة، False = محذوفة، None = لا يمكن الجزم (البوت فقد الصلاحية أو خطأ مؤقت).
    تليجرام لا يرسل تحديثاً عند حذف رسائل القنوات، فنحاول تعديل أزرار الرسالة بنفس الأزرار:
    «not modified» = موجودة، «not found» = محذوفة."""
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=promo_markup(ev_id))
        return True
    except Exception as e:
        s = str(e).lower()
        if "not modified" in s:
            return True
        if "message to edit not found" in s or "message_id_invalid" in s or "message not found" in s:
            return False
        return None


async def bot_can_post(bot, chat_id):
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
    except Exception:
        return False
    if me.status == "creator":
        return True
    return me.status == "administrator" and getattr(me, "can_post_messages", None) is not False


async def remove_entry(bot, db, e):
    """تُحذف رسالة الفعالية من القناة ⇒ يُزال المشترك من الفعالية ويُبلَّغ مع زر إرجاع."""
    async with _entry_lock:
        def fn(c):
            cur = c.execute("DELETE FROM event_entries WHERE event_id=? AND chat_id=? AND message_id=?",
                            (e["event_id"], e["chat_id"], e["message_id"]))
            if not cur.rowcount:
                return False
            c.execute("INSERT OR REPLACE INTO entry_removals(event_id,chat_id,user_id,title,members,removed_at,repost_msg_id) "
                      "VALUES(?,?,?,?,?,?,NULL)",
                      (e["event_id"], e["chat_id"], e["user_id"], e["title"], e["members"], now()))
            if not c.execute("SELECT 1 FROM event_entries WHERE event_id=? AND user_id=?",
                             (e["event_id"], e["user_id"])).fetchone():
                c.execute("DELETE FROM participants WHERE event_id=? AND user_id=?", (e["event_id"], e["user_id"]))
            return True
        if not await db.tx(fn):
            return False
    ev = await db.one("SELECT title FROM events WHERE id=?", e["event_id"])
    schedule_refresh(bot, db, e["event_id"])
    await audit(db, 0, "entry_removed", f"{e['event_id']}:{e['chat_id']}:{e['user_id']}")
    await safe_send(
        bot, e["user_id"],
        f"⚠️ <b>حُذفت رسالة الفعالية</b> «{esc(ev['title'] if ev else '')}» من قناتك «{esc(e['title'] or '')}»، "
        f"لذلك <b>أُزيلت مشاركتك</b> من الفعالية.\n\n"
        f"للعودة للروليت اضغط زر «🔄 إعادة الإرسال» بالأسفل.\n"
        f"⏳ أو يعيد البوت إرسال الرسالة في قناتك تلقائياً بعد 3 دقائق (بشرط أن يبقى مشرفاً بصلاحية النشر)، "
        f"وبعدها اضغط الزر ليعود اسمك للمسابقة.",
        reply_markup=return_markup(e["event_id"], e["chat_id"]))
    return True


async def restore_entry(bot, db, ev_id, chat_id, uid):
    """زر «إرجاع»: يعيد نشر الرسالة (أو يستخدم المنشورة تلقائياً) ويُعيد المشترك للمسابقة. يرجع (ok, نص)."""
    async with _entry_lock:
        rem = await db.one("SELECT * FROM entry_removals WHERE event_id=? AND chat_id=?", ev_id, chat_id)
        if not rem:
            return False, "لا يوجد طلب إرجاع لهذه القناة (ربما أُرجعت بالفعل)."
        if rem["user_id"] != uid:
            return False, "هذا الزر لصاحب المشاركة فقط."
        ev = await db.one("SELECT * FROM events WHERE id=?", ev_id)
        if not ev or ev["status"] != "active" or ev["end_at"] <= now():
            await db.run("DELETE FROM entry_removals WHERE event_id=? AND chat_id=?", ev_id, chat_id)
            return False, "انتهت الفعالية."
        if await db.val("SELECT 1 FROM event_entries WHERE event_id=? AND chat_id=?", ev_id, chat_id):
            return False, "هذي القناة مسجلة مسبقاً بالفعالية."
        if await check_user(bot, db, ev, uid):
            return False, "لم تعد تستوفي شروط الفعالية (مثل الاشتراك الإجباري). أكملها ثم اضغط «إرجاع»."
        try:
            m = await bot.get_chat_member(chat_id, uid)
            if m.status not in ("administrator", "creator"):
                return False, "لم تعد مشرفاً بهذه القناة."
        except Exception:
            return False, "تعذر التحقق من إشرافك بالقناة."
        if not await bot_can_post(bot, chat_id):
            return False, "❌ البوت ليس مشرفاً بالقناة أو بلا صلاحية نشر الرسائل. أعطه الصلاحية ثم اضغط «إرجاع» مرة ثانية."
        mid = rem["repost_msg_id"] or 0
        if not (mid and await msg_exists(bot, chat_id, mid, ev_id)):
            try:
                mid = (await bot.send_message(chat_id, promo_text(ev), reply_markup=promo_markup(ev_id))).message_id
            except Exception:
                return False, "❌ تعذر النشر بقناتك. تأكد أن البوت مشرف وله صلاحية نشر الرسائل."

        def fn(c):
            if not c.execute("DELETE FROM entry_removals WHERE event_id=? AND chat_id=?", (ev_id, chat_id)).rowcount:
                return False
            c.execute("INSERT OR IGNORE INTO event_entries(event_id,chat_id,user_id,title,members,message_id,created_at) "
                      "VALUES(?,?,?,?,?,?,?)", (ev_id, chat_id, uid, rem["title"], rem["members"], mid, now()))
            c.execute("INSERT OR IGNORE INTO participants(event_id,user_id,joined_at) VALUES(?,?,?)", (ev_id, uid, now()))
            return True
        await db.tx(fn)
    schedule_refresh(bot, db, ev_id)
    await check_join_goal(bot, db, ev_id)
    await audit(db, uid, "entry_restored", f"{ev_id}:{chat_id}")
    return True, "✅ أُعيدت الرسالة بقناتك وعدت للمسابقة."


async def repost_removed(bot, db):
    """بعد 3 دقائق من الحذف: إعادة نشر الرسالة بالقناة تلقائياً إن بقي البوت مشرفاً بصلاحية النشر."""
    for rem in await db.all("SELECT * FROM entry_removals WHERE repost_msg_id IS NULL AND removed_at<=?",
                            now() - REPOST_DELAY):
        async with _entry_lock:
            cur = await db.one("SELECT * FROM entry_removals WHERE event_id=? AND chat_id=?", rem["event_id"], rem["chat_id"])
            if not cur or cur["repost_msg_id"] is not None:
                continue   # أُرجعت بالزر أو عُولجت
            ev = await db.one("SELECT * FROM events WHERE id=?", rem["event_id"])
            if not ev or ev["status"] != "active" or ev["end_at"] <= now():
                await db.run("DELETE FROM entry_removals WHERE event_id=? AND chat_id=?", rem["event_id"], rem["chat_id"])
                continue
            mid = 0   # 0 = جرت المحاولة (فشلت أو لا صلاحية) فلا نكررها
            if await bot_can_post(bot, rem["chat_id"]):
                try:
                    mid = (await bot.send_message(rem["chat_id"], promo_text(ev),
                                                  reply_markup=promo_markup(ev["id"]))).message_id
                except Exception as e:
                    log.info("repost to %s failed: %s", rem["chat_id"], e)
            await db.run("UPDATE entry_removals SET repost_msg_id=? WHERE event_id=? AND chat_id=?",
                         mid, rem["event_id"], rem["chat_id"])
        if mid:
            await safe_send(bot, rem["user_id"],
                            f"📣 أعاد البوت نشر رسالة الفعالية «{esc(ev['title'])}» بقناتك «{esc(rem['title'] or '')}».\n"
                            f"لتعود للمسابقة اضغط «🔄 إعادة الإرسال» (ولا تحذف الرسالة مرة ثانية).",
                            reply_markup=return_markup(rem["event_id"], rem["chat_id"]))
        await asyncio.sleep(0.05)


async def watch_entries(bot, db):
    """دورة واحدة: كشف الرسائل المحذوفة ثم إعادة النشر التلقائي."""
    rows = await db.all(
        "SELECT en.* FROM event_entries en JOIN events ev ON ev.id=en.event_id "
        "WHERE ev.kind='chroulette' AND ev.status='active' AND ev.end_at>? AND en.message_id IS NOT NULL", now())
    for e in rows:
        if await msg_exists(bot, e["chat_id"], e["message_id"], e["event_id"]) is False:
            await remove_entry(bot, db, e)
        await asyncio.sleep(0.05)
    await repost_removed(bot, db)


async def entry_watcher(bot, db):
    while True:
        try:
            await watch_entries(bot, db)
        except Exception:
            log.exception("entry watcher error")
        await asyncio.sleep(WATCH_EVERY)


async def detect_bursts(bot, db):
    """رشق مشبوه: منظّم جلب عدداً كبيراً من المستخدمين خلال 10 دقائق."""
    limit = int(db.get("burst_limit"))
    rows = await db.all("SELECT owner_id, created_at/600 AS b, COUNT(*) AS n FROM referrals WHERE created_at>=? "
                        "GROUP BY owner_id, b HAVING n>=?", now() - 3600, limit)
    for r in rows:
        if await db.val("SELECT 1 FROM flags WHERE owner_id=?", r["owner_id"]):
            continue
        reason = f"{r['n']} إحالة خلال 10 دقائق"
        await db.run("INSERT OR IGNORE INTO flags(owner_id,reason,created_at) VALUES(?,?,?)", r["owner_id"], reason, now())
        for uid in OWNER_IDS:
            await safe_send(bot, uid, f"🚨 اشتباه رشق: <code>{r['owner_id']}</code> جلب {reason}.\n"
                                      f"راجع /user {r['owner_id']} قبل أي صرف. لإزالة العلامة: /unflag {r['owner_id']}")
    return len(rows)


async def owner_quality(db, owner_id):
    q = await db.one(
        """SELECT COUNT(*) AS total,
        SUM(CASE WHEN r.status='valid' THEN 1 ELSE 0 END) AS valid,
        SUM(CASE WHEN r.status='pending' THEN 1 ELSE 0 END) AS pending,
        SUM(CASE WHEN r.status='invalid' THEN 1 ELSE 0 END) AS invalid,
        SUM(CASE WHEN u.last_seen-u.created_at<120 THEN 1 ELSE 0 END) AS inactive
        FROM referrals r JOIN users u ON u.user_id=r.user_id WHERE r.owner_id=?""", owner_id)
    return {k: (v or 0) for k, v in q.items()}


async def retention_sample(bot, db, claim_id, n=60):
    """عيّنة عشوائية من الأعضاء المطلوب الدفع عنهم: كم منهم ما زال بالقنوات الإجبارية؟ → (فُحص, بقي)"""
    chans = await global_mandatory(db)
    if not chans:
        return None
    ids = [r["user_id"] for r in await db.all("SELECT user_id FROM referrals WHERE claim_id=? ORDER BY RANDOM() LIMIT ?",
                                              claim_id, n)]
    known = stayed = 0
    for uid in ids:
        res = [await is_member(bot, c["chat_id"], uid) for c in chans]
        if any(x is None for x in res):
            continue
        known += 1
        stayed += all(res)
        await asyncio.sleep(0.04)
    return known, stayed


async def weekly_post(bot, db):
    """ينشر أفضل المنظّمين أسبوعياً بقناتك الإجبارية (منافسة + ترويج للبوت)."""
    if db.get("weekly_post") != "1":
        return
    last = int(db.get("last_weekly"))
    if last == 0:
        return await db.set("last_weekly", now())
    if now() - last < 7 * 86400:
        return
    await db.set("last_weekly", now())
    chans = await global_mandatory(db)
    rows = await db.all("SELECT u.user_id,u.first_name,u.username, COUNT(*) AS n FROM referrals r JOIN users u "
                        "ON u.user_id=r.owner_id WHERE r.created_at>=? AND r.status IN ('pending','valid') "
                        "GROUP BY r.owner_id ORDER BY n DESC LIMIT 5", now() - 7 * 86400)
    if not chans or not rows:
        return
    lines = ["🏆 <b>أفضل المنظّمين هذا الأسبوع</b>\n"]
    lines += [f"{i}. {mention(r)} — {r['n']}" for i, r in enumerate(rows, 1)]
    lines.append(f"\n🎡 أنشئ روليتك وانمِ قناتك: https://t.me/{ctx.bot_username}")
    await safe_send(bot, chans[0]["chat_id"], "\n".join(lines))


async def daily_digest(bot, db):
    last = int(db.get("last_digest"))
    if last == 0:
        return await db.set("last_digest", now())
    if now() - last < 86400:
        return
    await db.set("last_digest", now())
    day = now() - 86400
    v = db.val
    txt = (f"🗞 <b>ملخص اليوم</b>\n👥 مستخدمون جدد: {await v('SELECT COUNT(*) FROM users WHERE created_at>=?', day)}\n"
           f"🔗 إحالات جديدة: {await v('SELECT COUNT(*) FROM referrals WHERE created_at>=?', day)}\n"
           f"🎯 فعاليات جديدة: {await v('SELECT COUNT(*) FROM events WHERE created_at>=?', day)} • "
           f"نشطة الآن: {await v('SELECT COUNT(*) FROM events WHERE status=?', 'active')}\n"
           f"💰 طلبات صرف معلقة: {await v('SELECT COUNT(*) FROM payouts WHERE status=?', 'pending')}\n"
           f"🚨 منظّمون مُعلَّمون: {await v('SELECT COUNT(*) FROM flags')}")
    for uid in OWNER_IDS:
        await safe_send(bot, uid, txt)


async def scheduler(bot, db):
    last_chan = last_thr = last_burst = 0
    while True:
        try:
            for e in await db.all("SELECT id,owner_id,title,draw_mode,draw_notified FROM events "
                                  "WHERE status='active' AND end_at<=?", now()):
                if is_manual(e):   # لا سحب تلقائي (لمالك البوت فقط): ننبّه المنظّم مرة واحدة
                    if not e["draw_notified"]:
                        await db.run("UPDATE events SET draw_notified=1 WHERE id=?", e["id"])
                        from aiogram.utils.keyboard import InlineKeyboardBuilder
                        kb = InlineKeyboardBuilder()
                        kb.button(text="🎯 اختيار الفائز بنفسي", callback_data=f"evpick:{e['id']}")
                        kb.button(text="🎡 تدوير العجلة (تلقائي)", callback_data=f"evend:{e['id']}")
                        kb.adjust(1)
                        await safe_send(bot, e["owner_id"],
                                        f"⏰ انتهى وقت «{esc(e['title'])}» (#{e['id']}).\nاختر طريقة تحديد الفائز:",
                                        reply_markup=kb.as_markup())
                    continue
                await finish_event(bot, db, e["id"])
            await send_reminders(bot, db)
            await validate_referrals(db, bot)
            if now() - last_burst > 600:
                last_burst = now()
                await detect_bursts(bot, db)
            if now() - last_thr > 600:
                last_thr = now()
                await notify_thresholds(bot, db)
            if now() - last_chan > 6 * 3600:
                last_chan = now()
                await check_channels(bot, db)
            await weekly_post(bot, db)
            await daily_digest(bot, db)
            await daily_backup(bot, db)
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(30)
