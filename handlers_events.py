from html import escape as esc

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ctx
from i18n import t
from handlers_user import fails_screen
from services import KIND_ICON, KIND_LABEL, audit, check_user, ensure_channel_link, event_text, notify_join, now
from ui import channel_url, home_kb, resolve_channel, show

router = Router()
NOT_CMD = ~F.text.startswith("/")


class New(StatesGroup):
    title = State()
    prize = State()
    promo = State()
    minm = State()
    winners = State()
    hours = State()
    channel = State()
    cond = State()
    cond_text = State()


class AddCh(StatesGroup):
    wait = State()


class Entry(StatesGroup):
    wait = State()


async def lang_of(uid):
    return await ctx.db.val("SELECT lang FROM users WHERE user_id=?", uid) or "ar"


# ───────────── إنشاء فعالية (معالج خطوة بخطوة) ─────────────
@router.callback_query(F.data == "m:new")
async def m_new(cb: CallbackQuery):
    lang = await lang_of(cb.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="🎡 روليت (إعداد كامل)", callback_data="new:roulette")
    kb.button(text="🏆 مسابقة أصوات (إعداد كامل)", callback_data="new:votes")
    kb.button(text="🌐 رولت القنوات (للمشرفين)", callback_data="new:chroulette")
    kb.button(text="⚡ روليت سريع: 24 ساعة • فائز 1", callback_data="tpl:r24")
    kb.button(text="⚡ روليت: 3 أيام • 3 فائزين", callback_data="tpl:r72")
    kb.button(text="⚡ أصوات: 7 أيام • 3 فائزين", callback_data="tpl:v7")
    kb.button(text=t(lang, "btn_home"), callback_data="m:home")
    kb.adjust(1)
    await show(cb, "➕ <b>إنشاء فعالية</b>\nاختر النوع أو قالب جاهز:", kb.as_markup())
    await cb.answer()


PRESETS = {
    "r24": {"kind": "roulette", "winners": 1, "hours": 24},
    "r72": {"kind": "roulette", "winners": 3, "hours": 72},
    "v7": {"kind": "votes", "winners": 3, "hours": 168},
}


@router.callback_query(F.data.startswith("new:") | F.data.startswith("tpl:"))
async def start_wizard(cb: CallbackQuery, state: FSMContext):
    db = ctx.db
    uid = cb.from_user.id
    lang = await lang_of(uid)
    kind, key = cb.data.split(":")
    data = dict(PRESETS[key]) if kind == "tpl" else {"kind": key}
    if db.get("feat_" + data["kind"]) != "1":
        return await cb.answer(t(lang, "feat_off"), show_alert=True)
    mx = int(db.get("max_active_events"))
    n = await db.val("SELECT COUNT(*) FROM events WHERE owner_id=? AND status='active'", uid)
    if n >= mx and not ctx.is_admin(uid):
        return await cb.answer(t(lang, "limit", n=mx), show_alert=True)
    data.update({"premium": False, "extra": []})
    await state.clear()
    await state.set_data(data)
    await cb.answer()
    await advance(cb.message, state)


async def advance(target, state: FSMContext):
    d = await state.get_data()
    if "title" not in d:
        await state.set_state(New.title)
        return await target.answer("✏️ اكتب عنوان الفعالية:\n(للإلغاء: /cancel)")
    if "prize" not in d:
        await state.set_state(New.prize)
        return await target.answer("🎁 اكتب الجائزة (أو أرسل - للتخطي):")
    if d["kind"] == "chroulette" and "promo" not in d:
        await state.set_state(New.promo)
        return await target.answer("📝 اكتب الرسالة اللي ينشرها البوت بقنوات المشاركين (إعلان قناتك أو جائزتك):")
    if "winners" not in d:
        await state.set_state(New.winners)
        kb = InlineKeyboardBuilder()
        for n in (1, 2, 3, 5, 10, 15):
            kb.button(text=str(n), callback_data=f"w:{n}")
        kb.adjust(6)
        return await target.answer("👑 كم عدد الفائزين؟ اضغط رقم أو اكتبه:", reply_markup=kb.as_markup())
    if "hours" not in d:
        await state.set_state(New.hours)
        kb = InlineKeyboardBuilder()
        for h, label in ((1, "ساعة"), (6, "6 ساعات"), (24, "يوم"), (72, "3 أيام"), (168, "أسبوع")):
            kb.button(text=label, callback_data=f"d:{h}")
        kb.adjust(3)
        return await target.answer("⏰ مدة الفعالية؟ اضغط أو اكتب عدد الساعات:", reply_markup=kb.as_markup())
    if "channel" not in d:
        await state.set_state(New.channel)
        rows = await ctx.db.all("SELECT chat_id,title FROM channels WHERE owner_id=? AND active=1", target.chat.id)
        kb = InlineKeyboardBuilder()
        for r in rows:
            kb.button(text=f"📺 {r['title']}", callback_data=f"pch:{r['chat_id']}")
        if d["kind"] == "chroulette":
            if not rows:
                await state.clear()
                return await target.answer("🌐 رولت القنوات يحتاج قناة نشر. أضف قناتك أولاً من «قنواتي» ثم أعد المحاولة.")
        else:
            kb.button(text="بدون قناة (رابط فقط)", callback_data="pch:0")
        kb.adjust(1)
        return await target.answer("📺 وين تنشر الفعالية؟\n(لإضافة قناة جديدة استخدم «قنواتي» من القائمة)",
                                   reply_markup=kb.as_markup())
    if d["kind"] == "chroulette" and "min_members" not in d:
        await state.set_state(New.minm)
        kb = InlineKeyboardBuilder()
        for n in (0, 100, 500, 1000, 5000):
            kb.button(text="بدون حد" if n == 0 else str(n), callback_data=f"mm:{n}")
        kb.adjust(3)
        return await target.answer("👥 أقل عدد أعضاء لقناة المشارك؟ اضغط أو اكتب رقماً:", reply_markup=kb.as_markup())
    await show_conditions(target, state)


@router.message(StateFilter(New.title), F.text, NOT_CMD)
async def in_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()[:100])
    await advance(m, state)


@router.message(StateFilter(New.prize), F.text, NOT_CMD)
async def in_prize(m: Message, state: FSMContext):
    txt = m.text.strip()
    await state.update_data(prize=None if txt == "-" else txt[:150])
    await advance(m, state)


@router.message(StateFilter(New.promo), F.text, NOT_CMD)
async def in_promo(m: Message, state: FSMContext):
    await state.update_data(promo=m.text.strip()[:800])
    await advance(m, state)


@router.callback_query(StateFilter(New.minm), F.data.startswith("mm:"))
async def cb_minm(cb: CallbackQuery, state: FSMContext):
    await state.update_data(min_members=int(cb.data[3:]))
    await cb.answer()
    await advance(cb.message, state)


@router.message(StateFilter(New.minm), F.text, NOT_CMD)
async def in_minm(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("اكتب رقماً (0 = بدون حد)")
    await state.update_data(min_members=int(m.text))
    await advance(m, state)


@router.callback_query(StateFilter(New.winners), F.data.startswith("w:"))
async def cb_winners(cb: CallbackQuery, state: FSMContext):
    await state.update_data(winners=int(cb.data[2:]))
    await cb.answer()
    await advance(cb.message, state)


@router.message(StateFilter(New.winners), F.text, NOT_CMD)
async def in_winners(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not 1 <= int(m.text) <= 100:
        return await m.answer("اكتب رقماً من 1 إلى 100")
    await state.update_data(winners=int(m.text))
    await advance(m, state)


@router.callback_query(StateFilter(New.hours), F.data.startswith("d:"))
async def cb_hours(cb: CallbackQuery, state: FSMContext):
    await state.update_data(hours=float(cb.data[2:]))
    await cb.answer()
    await advance(cb.message, state)


@router.message(StateFilter(New.hours), F.text, NOT_CMD)
async def in_hours(m: Message, state: FSMContext):
    try:
        h = float(m.text.replace(",", "."))
        assert 0.1 <= h <= 24 * 60
    except Exception:
        return await m.answer("اكتب عدد الساعات (مثلاً 12)")
    await state.update_data(hours=h)
    await advance(m, state)


@router.callback_query(StateFilter(New.channel), F.data.startswith("pch:"))
async def cb_channel(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data[4:])
    if cid:
        ch = await ctx.db.one("SELECT * FROM channels WHERE chat_id=? AND owner_id=?", cid, cb.from_user.id)
        if not ch:
            return await cb.answer("—", show_alert=True)
    await state.update_data(channel=cid or None)
    await cb.answer()
    await advance(cb.message, state)


# ───────────── شروط الفعالية ─────────────
async def show_conditions(target, state, edit=None):
    d = await state.get_data()
    await state.set_state(New.cond)
    lines = [f"📝 <b>{esc(d['title'])}</b>",
             f"{KIND_LABEL[d['kind']]} • 👑 {d['winners']} • ⏰ {d['hours']} ساعة", "",
             "📌 <b>الشروط الإضافية</b> (الاشتراك بقنوات المالك الإجبارية يُطبَّق تلقائياً):",
             f"💎 بريميوم: {'✅' if d.get('premium') else '❌'}",
             f"💬 تعليق: {esc(d['comment_text']) if d.get('comment_text') else '❌'}",
             f"🤖 تفعيل بوت: {'✅' if d.get('bot_url') else '❌'}",
             f"📢 قنوات إضافية: {len(d.get('extra', []))}"]
    if d["kind"] == "roulette":
        lines.append(f"🎟 فرص بالدعوة: {'✅ حتى ' + str(d['invite']) if d.get('invite') else '❌'}")
    if d["kind"] == "chroulette":
        lines.append(f"👥 أقل أعضاء بالقناة: {d.get('min_members', 0)}")
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 بريميوم", callback_data="c:prem")
    kb.button(text="💬 تعليق", callback_data="c:com")
    kb.button(text="🤖 تفعيل بوت", callback_data="c:bot")
    kb.button(text="📢 قناة اشتراك", callback_data="c:chan")
    if d["kind"] == "roulette":
        kb.button(text="🎟 فرص بالدعوة", callback_data="c:inv")
    kb.button(text="🚀 نشر الفعالية", callback_data="c:done")
    kb.button(text="❌ إلغاء", callback_data="c:cancel")
    kb.adjust(2, 2, 1, 1, 1)
    await target.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(StateFilter(New.cond), F.data.startswith("c:"))
async def cb_cond(cb: CallbackQuery, state: FSMContext, bot: Bot):
    act = cb.data[2:]
    d = await state.get_data()
    if act == "cancel":
        await state.clear()
        await cb.answer()
        return await cb.message.answer("تم الإلغاء.", reply_markup=home_kb(await lang_of(cb.from_user.id)))
    if act == "prem":
        await state.update_data(premium=not d.get("premium"))
        await cb.answer("✅")
        return await show_conditions(cb.message, state)
    if act == "inv":
        await state.update_data(invite=0 if d.get("invite") else 5)
        await cb.answer("✅")
        return await show_conditions(cb.message, state)
    if act == "com":
        if not d.get("channel"):
            return await cb.answer("اختر قناة للنشر أولاً (شرط التعليق يحتاج قناة).", show_alert=True)
        try:
            linked = (await bot.get_chat(d["channel"])).linked_chat_id
        except Exception:
            linked = None
        if not linked:
            return await cb.answer("هذي القناة ما لها مجموعة نقاش مرتبطة.", show_alert=True)
        await state.update_data(comment_chat=linked, ask="comment")
        await state.set_state(New.cond_text)
        await cb.answer()
        return await cb.message.answer("💬 اكتب الكلمة/الجملة المطلوب كتابتها بالتعليق.\n"
                                       "⚠️ لازم البوت يكون موجود بمجموعة النقاش ومعطّل خصوصية المجموعات من BotFather.")
    if act == "bot":
        await state.update_data(ask="bot")
        await state.set_state(New.cond_text)
        await cb.answer()
        return await cb.message.answer("🤖 أرسل رابط البوت المطلوب تفعيله (مثل https://t.me/SomeBot).\n"
                                       "ملاحظة: التفعيل يعتمد على ضغط المشارك زر «فعلتها» (لا يمكن التحقق منه تلقائياً).")
    if act == "chan":
        await state.update_data(ask="chan")
        await state.set_state(New.cond_text)
        await cb.answer()
        return await cb.message.answer("📢 أرسل @يوزر القناة أو حوّل منشوراً منها (لازم تكون مشرفاً والبوت مشرف فيها).")
    if act == "done":
        await cb.answer()
        return await publish(cb, state, bot)


@router.message(StateFilter(New.cond_text), F.text | F.forward_origin)
async def in_cond_text(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    ask = d.get("ask")
    if ask == "comment":
        await state.update_data(comment_text=m.text.strip()[:60])
    elif ask == "bot":
        url = (m.text or "").strip()
        if not url.startswith("https://t.me/"):
            return await m.answer("أرسل رابط يبدأ بـ https://t.me/")
        await state.update_data(bot_url=url)
    elif ask == "chan":
        chat, err = await resolve_channel(m, bot)
        if err:
            return await m.answer(err)
        url = await channel_url(bot, chat)
        extra = [c for c in d.get("extra", []) if c["chat_id"] != chat.id]
        extra.append({"chat_id": chat.id, "title": chat.title, "username": chat.username, "url": url})
        await state.update_data(extra=extra)
    await show_conditions(m, state)


async def publish(cb, state, bot):
    db = ctx.db
    d = await state.get_data()
    uid = cb.from_user.id
    eid = await db.insert(
        "INSERT INTO events(owner_id,kind,title,prize,winners,channel_id,created_at,end_at,need_premium,"
        "comment_chat,comment_text,need_bot_url,invite_bonus,promo_text,min_members,max_channels) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        uid, d["kind"], d["title"], d.get("prize"), d["winners"], d.get("channel"), now(),
        now() + int(d["hours"] * 3600), int(bool(d.get("premium"))), d.get("comment_chat"),
        d.get("comment_text"), d.get("bot_url"), int(d.get("invite", 0) or 0), d.get("promo"),
        int(d.get("min_members", 0)), int(ctx.db.get("chroulette_max_channels")))
    for ch in d.get("extra", []):
        await db.run("INSERT OR IGNORE INTO event_channels(event_id,chat_id,title,username,url) VALUES(?,?,?,?,?)",
                     eid, ch["chat_id"], ch["title"], ch["username"], ch["url"])
    link = f"https://t.me/{ctx.bot_username}?start=ev_{eid}"
    if d.get("channel"):
        ev = await db.one("SELECT * FROM events WHERE id=?", eid)
        kb = InlineKeyboardBuilder()
        if d["kind"] != "votes":
            kb.button(text="🎡 شارك الآن", url=link)
        else:
            kb.button(text="🙋 سجّل كمتسابق", url=link)
            kb.button(text="🗳 صوّت", url=link)
            kb.button(text="📊 الترتيب", callback_data=f"rank:{eid}")
        kb.adjust(1)
        try:
            msg = await bot.send_message(d["channel"], await event_text(db, ev), reply_markup=kb.as_markup())
            await db.run("UPDATE events SET message_id=? WHERE id=?", msg.message_id, eid)
        except Exception:
            await db.run("DELETE FROM events WHERE id=?", eid)
            await db.run("DELETE FROM event_channels WHERE event_id=?", eid)
            return await cb.message.answer("❌ تعذر النشر بالقناة. تأكد أن البوت مشرف وله صلاحية نشر الرسائل.")
    await state.clear()
    await audit(db, uid, "event_created", eid)
    lang = await lang_of(uid)
    kb = InlineKeyboardBuilder()
    kb.button(text="⚙️ إدارة الفعالية", callback_data=f"evm:{eid}")
    kb.button(text=t(lang, "btn_home"), callback_data="m:home")
    kb.adjust(1)
    await cb.message.answer(f"✅ تم إنشاء الفعالية #{eid}\n\n🔗 رابط المشاركة:\n{link}", reply_markup=kb.as_markup())


# ───────────── إدارة فعالياتي ─────────────
@router.callback_query(F.data == "m:events")
async def m_events(cb: CallbackQuery):
    lang = await lang_of(cb.from_user.id)
    rows = await ctx.db.all("SELECT id,kind,title,status FROM events WHERE owner_id=? ORDER BY id DESC LIMIT 10",
                            cb.from_user.id)
    kb = InlineKeyboardBuilder()
    icons = {"active": "🟢", "ended": "🏁", "cancelled": "⛔", "ending": "⏳"}
    for r in rows:
        kb.button(text=f"{icons.get(r['status'], '')} {KIND_ICON.get(r['kind'], '')} {r['title']}",
                  callback_data=f"evm:{r['id']}")
    kb.button(text=t(lang, "btn_home"), callback_data="m:home")
    kb.adjust(1)
    await show(cb, "📋 <b>فعالياتك</b>" + ("" if rows else "\n\nما عندك فعاليات بعد."), kb.as_markup())
    await cb.answer()


async def owned_event(cb, ev_id):
    ev = await ctx.db.one("SELECT * FROM events WHERE id=?", ev_id)
    if not ev or (ev["owner_id"] != cb.from_user.id and not ctx.is_admin(cb.from_user.id)):
        await cb.answer("غير مسموح", show_alert=True)
        return None
    return ev


@router.callback_query(F.data.startswith("evm:"))
async def ev_manage(cb: CallbackQuery):
    db = ctx.db
    ev = await owned_event(cb, int(cb.data[4:]))
    if not ev:
        return
    cnt = await db.val("SELECT COUNT(*) FROM participants WHERE event_id=?", ev["id"]) if ev["kind"] != "votes" \
        else await db.val("SELECT COUNT(*) FROM candidates WHERE event_id=?", ev["id"])
    left = max(0, ev["end_at"] - now())
    link = f"https://t.me/{ctx.bot_username}?start=ev_{ev['id']}"
    txt = (f"⚙️ <b>{esc(ev['title'])}</b> (#{ev['id']})\nالحالة: {ev['status']}\n"
           f"{'👥 المشاركون' if ev['kind'] != 'votes' else '🙋 المتسابقون'}: {cnt}\n"
           f"⏳ المتبقي: {left // 3600} ساعة {left % 3600 // 60} دقيقة\n🔗 {link}")
    kb = InlineKeyboardBuilder()
    if ev["status"] == "active":
        kb.button(text="🏁 إنهاء وإعلان الآن", callback_data=f"evend:{ev['id']}")
        kb.button(text="⏱ +1 ساعة", callback_data=f"evext:{ev['id']}:1")
        kb.button(text="⏱ +24 ساعة", callback_data=f"evext:{ev['id']}:24")
        kb.button(text="❌ إلغاء الفعالية", callback_data=f"evcan:{ev['id']}")
        if ev["kind"] != "votes":
            kb.button(text=f"🔔 إشعارات الانضمام: {'✅' if ev['notify_owner'] else '❌'}", callback_data=f"evnote:{ev['id']}")
    kb.button(text="⬅️", callback_data="m:events")
    kb.adjust(1)
    await show(cb, txt, kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("evend:"))
async def ev_end(cb: CallbackQuery, bot: Bot):
    from services import finish_event
    ev = await owned_event(cb, int(cb.data[6:]))
    if not ev:
        return
    await cb.answer("جاري السحب...")
    await finish_event(bot, ctx.db, ev["id"])


@router.callback_query(F.data.startswith("evext:"))
async def ev_ext(cb: CallbackQuery):
    _, eid, h = cb.data.split(":")
    ev = await owned_event(cb, int(eid))
    if not ev:
        return
    await ctx.db.run("UPDATE events SET end_at=end_at+?, reminded=0 WHERE id=? AND status='active'", int(h) * 3600, ev["id"])
    await audit(ctx.db, cb.from_user.id, "event_extended", f"{eid}+{h}h")
    await cb.answer("✅ تم التمديد")


@router.callback_query(F.data.startswith("evcan:"))
async def ev_cancel(cb: CallbackQuery):
    ev = await owned_event(cb, int(cb.data[6:]))
    if not ev:
        return
    await ctx.db.run("UPDATE events SET status='cancelled' WHERE id=? AND status='active'", ev["id"])
    await audit(ctx.db, cb.from_user.id, "event_cancelled", ev["id"])
    await cb.answer("تم الإلغاء", show_alert=True)


# ───────────── قنواتي ─────────────
@router.callback_query(F.data == "m:channels")
async def m_channels(cb: CallbackQuery):
    lang = await lang_of(cb.from_user.id)
    rows = await ctx.db.all("SELECT chat_id,title,active FROM channels WHERE owner_id=?", cb.from_user.id)
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"🗑 {r['title']} {'' if r['active'] else '(غير نشطة)'}", callback_data=f"chdel:{r['chat_id']}")
    kb.button(text="➕ إضافة قناة", callback_data="ch:add")
    kb.button(text=t(lang, "btn_home"), callback_data="m:home")
    kb.adjust(1)
    txt = ("📺 <b>قنواتك</b>\nأضف قناتك لتظهر بالأقسام وتُحسب لها الأعضاء الجدد.\n"
           "شرط: البوت مشرف بالقناة وأنت مشرف فيها." + ("" if rows else "\n\nما أضفت قنوات بعد."))
    await show(cb, txt, kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "ch:add")
async def ch_add(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddCh.wait)
    await cb.answer()
    await cb.message.answer("أرسل @يوزر القناة أو حوّل لي منشوراً منها:\n(للإلغاء: /cancel)")


@router.message(StateFilter(AddCh.wait), F.text | F.forward_origin)
async def ch_add_msg(m: Message, state: FSMContext, bot: Bot):
    db = ctx.db
    chat, err = await resolve_channel(m, bot)
    if err:
        return await m.answer(err)
    row = await db.one("SELECT owner_id FROM channels WHERE chat_id=?", chat.id)
    if row and row["owner_id"] != m.from_user.id:
        return await m.answer("⚠️ هذي القناة مسجلة بحساب آخر. تواصل مع المالك إذا هي قناتك.")
    await db.run("INSERT INTO channels(chat_id,owner_id,title,username,active,added_at) VALUES(?,?,?,?,1,?) "
                 "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username, active=1",
                 chat.id, m.from_user.id, chat.title, chat.username, now())
    await ensure_channel_link(db, m.from_user.id, chat.id, chat.title)
    await state.clear()
    await m.answer(f"✅ تمت إضافة «{esc(chat.title or '')}». صار لها رابط جلب خاص في «روابطي ومكافآتي».",
                   reply_markup=home_kb(await lang_of(m.from_user.id)))


@router.callback_query(F.data.startswith("chdel:"))
async def ch_del(cb: CallbackQuery):
    await ctx.db.run("DELETE FROM channels WHERE chat_id=? AND owner_id=?", int(cb.data[6:]), cb.from_user.id)
    await cb.answer("🗑 تم الحذف")
    await m_channels(cb)


@router.callback_query(F.data.startswith("evnote:"))
async def ev_note(cb: CallbackQuery):
    ev = await owned_event(cb, int(cb.data[7:]))
    if not ev:
        return
    await ctx.db.run("UPDATE events SET notify_owner=1-notify_owner WHERE id=?", ev["id"])
    await cb.answer("تم التبديل")


# ───────────── رولت القنوات: مشاركة المشرفين بقنواتهم ─────────────
@router.callback_query(F.data.startswith("cadd:"))
async def cadd(cb: CallbackQuery, state: FSMContext, bot: Bot):
    db, uid = ctx.db, cb.from_user.id
    await db.touch_user(cb.from_user)
    ev = await db.one("SELECT * FROM events WHERE id=?", int(cb.data[5:]))
    if not ev or ev["kind"] != "chroulette" or ev["status"] != "active" or ev["end_at"] <= now():
        return await cb.answer("انتهت الفعالية أو غير موجودة.", show_alert=True)
    fails = await check_user(bot, db, ev, uid)
    if fails:
        return await fails_screen(cb, ev, fails, cb.data)
    try:   # مشرفو/مالكو قناة الروليت نفسها ممنوعون من المشاركة
        m = await bot.get_chat_member(ev["channel_id"], uid)
        if m.status in ("administrator", "creator"):
            return await cb.answer("🚫 مشرفو قناة الروليت لا يشاركون فيه.", show_alert=True)
    except Exception:
        pass
    n = await db.val("SELECT COUNT(*) FROM event_entries WHERE event_id=? AND user_id=?", ev["id"], uid)
    if n >= ev["max_channels"]:
        return await cb.answer(f"وصلت للحد الأقصى ({ev['max_channels']} قنوات).", show_alert=True)
    await state.clear()
    await state.set_state(Entry.wait)
    await state.update_data(ev_id=ev["id"])
    await cb.answer()
    need = f"\n👥 أقل عدد أعضاء: {ev['min_members']}" if ev["min_members"] else ""
    await cb.message.answer("📺 أرسل @يوزر قناتك أو حوّل منشوراً منها.\n"
                            "الشروط: البوت مشرف بالقناة (مع صلاحية النشر) وأنت مشرف أو مالك لها." + need +
                            "\nسينشر البوت رسالة الفعالية بقناتك فوراً. (للإلغاء: /cancel)")


@router.message(StateFilter(Entry.wait), F.text | F.forward_origin)
async def entry_msg(m: Message, state: FSMContext, bot: Bot):
    db, uid = ctx.db, m.from_user.id
    d = await state.get_data()
    ev = await db.one("SELECT * FROM events WHERE id=?", d.get("ev_id", 0))
    if not ev or ev["status"] != "active" or ev["end_at"] <= now():
        await state.clear()
        return await m.answer("انتهت الفعالية.")
    chat, err = await resolve_channel(m, bot)
    if err:
        return await m.answer(err)
    if chat.id == ev["channel_id"]:
        return await m.answer("🚫 هذي قناة الروليت نفسها.")
    if await db.val("SELECT 1 FROM event_entries WHERE event_id=? AND chat_id=?", ev["id"], chat.id):
        return await m.answer("⚠️ هذي القناة مسجلة مسبقاً بالفعالية.")
    mine = await db.val("SELECT COUNT(*) FROM event_entries WHERE event_id=? AND user_id=?", ev["id"], uid)
    if mine >= ev["max_channels"]:
        await state.clear()
        return await m.answer("وصلت للحد الأقصى من القنوات.")
    try:
        members = await bot.get_chat_member_count(chat.id)
    except Exception:
        members = 0
    if members < ev["min_members"]:
        return await m.answer(f"❌ أعضاء القناة ({members}) أقل من الحد الأدنى ({ev['min_members']}).")
    kb = InlineKeyboardBuilder()
    kb.button(text="🎡 شارك بالروليت", url=f"https://t.me/{ctx.bot_username}?start=ev_{ev['id']}")
    try:   # ينشر الرسالة التي حددها صاحب الروليت بقناة المشارك
        msg = await bot.send_message(chat.id, esc(ev["promo_text"] or ev["title"]), reply_markup=kb.as_markup())
    except Exception:
        return await m.answer("❌ تعذر النشر بقناتك. تأكد أن البوت مشرف وله صلاحية نشر الرسائل.")
    await db.run("INSERT OR IGNORE INTO event_entries(event_id,chat_id,user_id,title,members,message_id,created_at) "
                 "VALUES(?,?,?,?,?,?,?)", ev["id"], chat.id, uid, chat.title, members, msg.message_id, now())
    await db.run("INSERT OR IGNORE INTO participants(event_id,user_id,joined_at) VALUES(?,?,?)", ev["id"], uid, now())
    await state.clear()
    cnt = mine + 1
    await notify_join(bot, db, ev, uid, extra=f"\n📺 القناة: {esc(chat.title or '')} ({members} عضو) • فرصه الآن: {cnt}")
    kb2 = InlineKeyboardBuilder()
    if cnt < ev["max_channels"]:
        kb2.button(text="➕ أضف قناة ثانية (فرصة إضافية)", callback_data=f"cadd:{ev['id']}")
    kb2.button(text="🏠", callback_data="m:home")
    kb2.adjust(1)
    await m.answer(f"✅ نُشرت الرسالة بقناتك «{esc(chat.title or '')}» وسُجّلت بالفعالية.\n🎟 فرصك الآن: {cnt}",
                   reply_markup=kb2.as_markup())


@router.callback_query(F.data.startswith("cmine:"))
async def cmine(cb: CallbackQuery):
    ev_id = int(cb.data[6:])
    rows = await ctx.db.all("SELECT title, members FROM event_entries WHERE event_id=? AND user_id=?", ev_id, cb.from_user.id)
    txt = f"🎟 فرصك: {len(rows)}\n" + "\n".join(f"• {esc(r['title'] or '')} ({r['members']})" for r in rows) if rows else "ما سجّلت أي قناة بعد."
    await cb.message.answer(txt)
    await cb.answer()
