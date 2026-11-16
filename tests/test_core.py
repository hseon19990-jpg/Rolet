"""اختبارات المنطق الأساسي (بدون تليجرام): python -m tests.test_core"""
import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import services as S  # noqa: E402
from db import DB  # noqa: E402


class FakeBot:
    id = 1

    def __init__(self):
        self.sent = []
        self.members = {}   # (chat, user) -> status

    async def get_chat_member(self, chat, uid):
        st = self.members.get((chat, uid), "left")
        return NS(status=st, is_member=False)

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text))
        return NS(message_id=len(self.sent))

    async def edit_message_text(self, *a, **kw):
        return None

    async def edit_message_reply_markup(self, *a, **kw):
        return None


_real_sleep = asyncio.sleep


async def _fast_sleep(*_):
    await _real_sleep(0)


def user(i, premium=False):
    return NS(id=i, username=f"u{i}", first_name=f"User{i}", language_code="ar", is_premium=premium)


async def main():
    path = tempfile.mktemp(suffix=".db")
    db = DB(path)
    bot = FakeBot()
    S.asyncio = NS(sleep=_fast_sleep, **{k: getattr(asyncio, k) for k in ('to_thread',)})  # تسريع الاختبار

    # 1) إحالة فريدة مرة وحدة فقط
    code = await S.ensure_default_link(db, 100)
    assert code == await S.ensure_default_link(db, 100)
    src = await S.resolve_source(db, f"ref_{code}")
    assert src[0] == 100
    for i in range(1, 11):
        assert await db.touch_user(user(i)) is True
        assert await S.record_referral(db, i, src) is True
    assert await db.touch_user(user(1)) is False            # مستخدم قديم
    assert await S.record_referral(db, 1, src) is False      # ما ينحسب مرتين
    other = await S.ensure_default_link(db, 200)
    assert await S.record_referral(db, 1, await S.resolve_source(db, f"ref_{other}")) is False  # ما ينتقل لمالك ثاني
    assert await S.record_referral(db, 100, src) is False    # إحالة ذاتية ممنوعة
    print("✓ unique referral + self-referral")

    # 2) الاعتماد: لازم تحقق + مرور الأيام + عدم حظر البوت
    await db.set("hold_days", "0")
    await db.run("UPDATE users SET verified=1 WHERE user_id<=8")      # 9,10 ما تحققوا
    await db.run("UPDATE users SET blocked_bot=1 WHERE user_id=8")    # 8 حظر البوت
    await db.run("UPDATE users SET banned=1 WHERE user_id=7")
    await S.validate_referrals(db)
    st = {r["user_id"]: r["status"] for r in await db.all("SELECT user_id,status FROM referrals")}
    assert [st[i] for i in range(1, 7)] == ["valid"] * 6, st
    assert st[7] == "invalid" and st[8] == "invalid", st
    assert st[9] == "invalid" and st[10] == "invalid", st   # غير متحققين مع hold=0
    print("✓ validation rules")

    # 3) الصرف: كل 3 معتمدين = 5$ (نغيّر العتبة للاختبار)
    await db.set("ref_threshold", "3")
    await db.set("ref_reward", "5")
    p = await S.create_payout(db, 100)
    assert p["amount"] == 10.0 and p["count"] == 6, p
    assert await S.create_payout(db, 100) == "exists"
    assert await db.val("SELECT COUNT(*) FROM referrals WHERE claim_id IS NOT NULL") == 6
    r = await S.decide_payout(db, p["id"], "reject", 1)
    assert r["status"] == "rejected"
    assert await db.val("SELECT COUNT(*) FROM referrals WHERE claim_id IS NOT NULL") == 0   # رجعت للرصيد
    p2 = await S.create_payout(db, 100)
    assert (await S.decide_payout(db, p2["id"], "paid", 1)) == "bad_state"
    await S.decide_payout(db, p2["id"], "approve", 1)
    assert (await S.decide_payout(db, p2["id"], "paid", 1))["status"] == "paid"
    assert await S.create_payout(db, 100) is None            # لا يوجد مستحق جديد
    print("✓ payout claim/reject/approve/paid")

    # 4) سعر خاص للرابط
    await db.run("UPDATE invite_links SET price=2.5, threshold=2 WHERE code=?", other)
    for i in range(20, 24):
        await db.touch_user(user(i))
        await db.run("UPDATE users SET verified=1 WHERE user_id=?", i)
        await S.record_referral(db, i, await S.resolve_source(db, f"ref_{other}"))
    await S.validate_referrals(db)
    p3 = await S.create_payout(db, 200)
    assert p3["amount"] == 5.0 and p3["count"] == 4, p3
    print("✓ per-link price/threshold")

    # 5) الروليت: سحب موثّق + استبدال الفائز المخالف
    await db.set("ref_threshold", "1500")
    eid = await db.insert("INSERT INTO events(owner_id,kind,title,winners,created_at,end_at) VALUES(?,?,?,?,?,?)",
                          100, "roulette", "Test", 2, S.now(), S.now() + 10)
    await db.run("INSERT INTO event_channels(event_id,chat_id,title) VALUES(?,?,?)", eid, -500, "Chan")
    for i in range(1, 7):
        await db.run("INSERT INTO participants VALUES(?,?,?)", eid, i, S.now())
        bot.members[(-500, i)] = "member"
    await db.run("UPDATE users SET banned=0, blocked_bot=0")
    bot.members[(-500, 3)] = "left"          # خرج من القناة قبل السحب
    ws = await S.finish_event(bot, db, eid)
    assert len(ws) == 2 and 3 not in ws, ws
    assert await S.finish_event(bot, db, eid) is None   # ما ينسحب مرتين
    proof = await db.val("SELECT proof FROM events WHERE id=?", eid)
    n, digest, seed = proof.split(":")
    assert n == "6" and S.draw_order(list(range(1, 7)), seed)[:3].count(3) <= 1
    print("✓ roulette draw + replacement + proof", proof)

    # 6) مسابقة الأصوات
    eid2 = await db.insert("INSERT INTO events(owner_id,kind,title,winners,created_at,end_at) VALUES(?,?,?,?,?,?)",
                           100, "votes", "V", 1, S.now(), S.now() + 10)
    a = await db.insert("INSERT INTO candidates(event_id,user_id,name,created_at) VALUES(?,?,?,?)", eid2, 1, "A", S.now())
    b = await db.insert("INSERT INTO candidates(event_id,user_id,name,created_at) VALUES(?,?,?,?)", eid2, 2, "B", S.now())
    for v, c in ((4, a), (5, b), (6, b)):
        await db.run("INSERT INTO votes VALUES(?,?,?,?)", eid2, v, c, S.now())
    assert await db.run("INSERT OR IGNORE INTO votes VALUES(?,?,?,?)", eid2, 4, b, S.now()) == 0  # صوت واحد فقط
    ws = await S.finish_event(bot, db, eid2)
    assert ws == [2], ws
    print("✓ votes: one vote per user, winner by count")

    # 7) كشف الصعود المفاجئ
    await db.set("vote_spike", "2")
    ev = await db.one("SELECT * FROM events WHERE id=?", eid2)
    await S.check_spike(bot, db, ev, b)
    assert any("مشبوه" in m for _, m in bot.sent)
    print("✓ vote spike alert")

    # 8) البوابة العامة: لا استخدام بدون اشتراك
    await db.run("INSERT INTO mandatory_channels(chat_id,title,url,added_at) VALUES(?,?,?,?)", -900, "Main", "https://t.me/main", S.now())
    miss = await S.gate_missing(bot, db, 77)
    assert [c["chat_id"] for c in miss] == [-900]
    bot.members[(-900, 77)] = "member"
    assert await S.gate_missing(bot, db, 77) == []            # اشترك → مسموح (ويُخزَّن مؤقتاً)
    bot.members[(-900, 77)] = "left"
    assert await S.gate_missing(bot, db, 77) == []            # الكاش ما زال صالحاً
    S.clear_gate(77)
    assert len(await S.gate_missing(bot, db, 77)) == 1        # بعد مسح الكاش يُمنع من جديد
    await db.set("gate_on", "0")
    assert await S.gate_missing(bot, db, 77) == []
    await db.set("gate_on", "1")
    print("✓ global subscription gate")

    # 9) الاعتماد يتطلب بقاء العضو بالقناة الإجبارية (ضد الرشق)
    await db.set("hold_days", "0")
    code9 = await S.ensure_default_link(db, 900)
    for i in (30, 31):
        await db.touch_user(user(i))
        await db.run("UPDATE users SET verified=1 WHERE user_id=?", i)
        await S.record_referral(db, i, await S.resolve_source(db, f"ref_{code9}"))
    bot.members[(-900, 30)] = "left"
    bot.members[(-900, 31)] = "member"
    await S.validate_referrals(db, bot)
    st = {r["user_id"]: r["status"] for r in await db.all("SELECT user_id,status FROM referrals WHERE owner_id=900")}
    assert st == {30: "invalid", 31: "valid"}, st
    print("✓ referral requires staying subscribed")

    # 10) كشف الرشق
    await db.set("burst_limit", "3")
    code10 = await S.ensure_default_link(db, 950)
    for i in range(40, 45):
        await db.touch_user(user(i))
        await S.record_referral(db, i, await S.resolve_source(db, f"ref_{code10}"))
    assert await S.detect_bursts(bot, db) >= 1
    assert await db.val("SELECT reason FROM flags WHERE owner_id=950")
    q = await S.owner_quality(db, 950)
    assert q["total"] == 5 and q["pending"] == 5
    print("✓ burst detection + quality report")

    # 11) فرص أكثر = احتمال أعلى (10 فرص مقابل 10 أشخاص بفرصة)
    ids = list(range(1, 12))
    first = sum(1 for sd in range(600) if S.draw_order(ids, f"s{sd}", {1: 10})[0] == 1)
    assert 0.38 < first / 600 < 0.62, first / 600      # المتوقع ≈ 50%
    assert S.draw_order(ids, "x", {1: 10}) == S.draw_order(ids, "x", {1: 10})   # قابل للتحقق
    print("✓ weighted tickets", round(first / 600, 2))

    # 12) رولت القنوات: القنوات = فرص، ومن فقد الإشراف يُستبعد
    await db.run("DELETE FROM mandatory_channels")
    S.GATE_CACHE.clear()
    e3 = await db.insert("INSERT INTO events(owner_id,kind,title,winners,channel_id,created_at,end_at,max_channels,min_members) "
                         "VALUES(?,?,?,?,?,?,?,?,?)", 100, "chroulette", "CH", 1, -777, S.now(), S.now() + 10, 10, 0)
    for chat, uid_ in ((-1, 1), (-2, 1), (-3, 1), (-4, 2)):
        await db.run("INSERT INTO event_entries VALUES(?,?,?,?,?,?,?)", e3, chat, uid_, "c", 500, 1, S.now())
        bot.members[(chat, uid_)] = "administrator"
    for chat in (-1, -2, -3):
        bot.members[(chat, 1)] = "left"                       # المستخدم 1 لم يعد مشرفاً
    ws = await S.finish_event(bot, db, e3)
    assert ws == [2], ws
    print("✓ channel roulette: tickets by channels + admin re-check")

    # 13) صيغة رابط الإشعار
    link = S.open_link({"user_id": 555, "first_name": "Ali", "username": "ali_x"})
    assert 'href="tg://openmessage?user_id=555"' in link and "@ali_x" in link and "555" in link
    print("✓ join-notification link format")
    print("ALL OK")


asyncio.run(main())
