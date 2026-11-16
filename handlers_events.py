from html import escape as esc

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ctx
from i18n import t
from handlers_user import fails_screen
from services import (KIND_ICON, KIND_LABEL, audit, check_user, ensure_channel_link, event_text, notify_join, now, post_markup, post_text, refresh_post, schedule_refresh, is_manual, check_join_goal,
                      promo_markup, promo_text, restore_entry, return_markup)
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
    goal = State()
    channel = State()
    cond = State()
    cond_text = State()
    addch = State()
    mode = State()


class Pick(StatesGroup):
    sel = State()


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
    "r24": {"kind": "roulette", "winners": 1, "hours": 24, "draw_mode": "auto"},
    "r72": {"kind": "roulette", "winners": 3, "hours": 72, "draw_mode": "auto"},
    "v7": {"kind": "votes", "winners": 3, "hours": 168, "draw_mode": "auto"},
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
        kb.button(text="♾ مفتوح (أنهيها بنفسي)", callback_data="d:0")
        return await target.answer("⏰ كم ساعة تستمر الفعالية؟\nأرسل <b>رقماً</b> (عدد الساعات) أو اضغط «مفتوح»:",
                                   reply_markup=kb.as_markup())
    if "goal" not in d:
        await state.set_state(New.goal)
        kb = InlineKeyboardBuilder()
        kb.button(text="♾ مفتوح (بدون عدد محدد)", callback_data="g:0")
        if d.get("kind") == "votes":
            q = ("🗳 كم عدد أصوات الفائز؟\nتنتهي المسابقة عندما يصل أحد المتسابقين لهذا العدد (أو بانتهاء الوقت، أيهما أسبق).\n")
        else:
            q = ("👥 كم عدد المنضمين المطلوب؟\nتنتهي الفعالية ويُسحب الفائز عند وصول المنضمين لهذا العدد (أو بانتهاء الوقت، أيهما أسبق).\n")
        return await target.answer(q + "أرسل <b>رقماً</b> أو اضغط «مفتوح»:", reply_markup=kb.as_markup())
    if "draw_mode" not in d and not ctx.is_owner(state.key.user_id):
        await state.update_data(draw_mode="auto")   # غير المالك: سحب عشوائي دائماً
        d["draw_mode"] = "auto"
    if "draw_mode" not in d:
        await state.set_state(New.mode)
        kb = InlineKeyboardBuilder()
        kb.button(text="🎡 تلقائي (سحب عشوائي عند انتهاء الوقت)", callback_data="dm:auto")
        kb.button(text="🎯 يدوي (أنا أحدد الفائز وأنهي متى أريد)", callback_data="dm:manual")
        kb.adjust(1)
        return await target.answer("🎯 كيف يتحدد الفائز؟\n(تقدر بكل الأحوال تدوّر العجلة أو تنهي الفعالية يدوياً قبل الوقت)",
                                   reply_markup=kb.as_markup())
    if d.get("kind") == "votes" and "approve" not in d:
        await state.set_state(New.mode)
        kb = InlineKeyboardBuilder()
        kb.button(text="⚡ مباشرة (يدخل المتسابق فوراً)", callback_data="ap:0")
        kb.button(text="✋ بموافقتي أو موافقة المشرف", callback_data="ap:1")
        kb.adjust(1)
        return await target.answer("🙋 كيف يدخل المتسابقون؟", reply_markup=kb.as_markup())
    if "channel" not in d:
        await state.set_state(New.channel)
        rows = await ctx.db.all("SELECT chat_id,title FROM channels WHERE owner_id=? AND active=1", target.chat.id)
        kb = InlineKeyboardBuilder()
        for r in rows:
            kb.button(text=f"📺 {r['title']}", callback_data=f"pch:{r['chat_id']}")
        kb.button(text="➕ إضافة قناة", callback_data="wch:add")
        kb.adjust(1)
        txt = "📺 اختر القناة اللي ينشر فيها البوت الفعالية:" if rows else \
            "📺 ما عندك قنوات مضافة. اضغط «إضافة قناة» (لازم البوت مشرف فيها وأنت مشرف أيضاً)."
        return await target.answer(txt, reply_markup=kb.as_markup())
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
    h = float(cb.data[2:])
    if h == 0:   # وقت مفتوح: لا ينتهي تلقائياً
        if ctx.is_owner(cb.from_user.id):
            await state.update_data(hours=24 * 365 * 10, draw_mode="manual")
        else:
            await state.update_data(hours=24 * 365 * 10)
    else:
        await state.update_data(hours=h)
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


@router.callback_query(StateFilter(New.goal), F.data.startswith("g:"))
async def cb_goal(cb: CallbackQuery, state: FSMContext):
    await state.update_data(goal=0)
    await cb.answer()
    await advance(cb.message, state)


@router.message(StateFilter(New.goal), F.text, NOT_CMD)
async def in_goal(m: Message, state: FSMContext):
    txt = m.text.strip()
    if not txt.isdigit() or not 1 <= int(txt) <= 10_000_000:
        return await m.answer("اكتب رقماً صحيحاً (عدد الأصوات)، أو اضغط «مفتوح».")
    await state.update_data(goal=int(txt))
    await advance(m, state)


@router.callback_query(StateFilter(New.mode), F.data.startswith("ap:"))
async def cb_approve_mode(cb: CallbackQuery, state: FSMContext):
    await state.update_data(approve=1 if cb.data == "ap:1" else 0)
    await cb.answer()
    await advance(cb.message, state)


@router.callback_query(StateFilter(New.mode), F.data.startswith("dm:"))
async def cb_mode(cb: CallbackQuery, state: FSMContext):
    mode = cb.data[3:] if cb.data[3:] in ("auto", "manual") else "auto"
    await state.update_data(draw_mode=mode if ctx.is_owner(cb.from_user.id) else "auto")
    await cb.answer()
    await advance(cb.message, state)


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


@router.callback_query(StateFilter(New.channel), F.data == "wch:add")
async def wch_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(New.addch)
    await cb.answer()
    await cb.message.answer("➕ أرسل @يوزر القناة أو رابطها أو حوّل منشوراً منها.\n"
                            "⚠️ لازم البوت يكون <b>مشرفاً</b> فيها مع صلاحية النشر، وأنت مشرف فيها.\n(للإلغاء: /cancel)")


@router.message(StateFilter(New.addch), F.text | F.forward_origin)
async def wch_add_msg(m: Message, state: FSMContext, bot: Bot):
    chat, err = await resolve_channel(m, bot)
    if err:
        return await m.answer(err)
    err = await register_channel(m.from_user.id, chat)
    if err:
        return await m.answer(err)
    await m.answer(f"✅ تمت إضافة «{esc(chat.title or '')}» واعتمادها لنشر الفعالية.")
    await state.update_data(channel=chat.id)
    await advance(m, state)


# ───────────── شروط الفعالية ─────────────
async def show_conditions(target, state, edit=None):
    d = await state.get_data()
    await state.set_state(New.cond)
    lines = [f"📝 <b>{esc(d['title'])}</b>",
             f"{KIND_LABEL[d['kind']]} • 👑 {d['winners']} • ⏰ " + ("مفتوح" if d['hours'] > 24 * 365 else f"{d['hours']} ساعة")
             + ((f" • 🗳 {d['goal']} صوت" if d["kind"] == "votes" else f" • 👥 {d['goal']} منضم") if d.get("goal") else ""), "",
             "📌 <b>الشروط الإضافية</b> (الاشتراك بقنوات المالك الإجبارية يُطبَّق تلقائياً):",
             f"💎 بريميوم: {'✅' if d.get('premium') else '❌'}",
             f"💬 تعليق: {'✅' if (d.get('comment_chat') or d.get('comment_url')) else '❌'}",
             f"🤖 تفعيل بوت: {'✅' if d.get('bot_url') else '❌'}",
             f"🗳 تصويت (رابط التعليق): {'✅' if d.get('vote_url') else '❌'}",
             f"📢 قنوات إضافية: {len(d.get('extra', []))}"]
    if d["kind"] == "roulette":
        lines.append(f"🎟 فرص بالدعوة: {'✅ حتى ' + str(d['invite']) if d.get('invite') else '❌'}")
    if d["kind"] == "chroulette":
        lines.append(f"👥 أقل أعضاء بالقناة: {d.get('min_members', 0)}")
    if ctx.is_owner(state.key.user_id):   # يظهر لمالك البوت فقط، وبالخاص
        lines.insert(5, f"🎯 تحديد الفائز: {'يدوي' if d.get('draw_mode') == 'manual' else 'عشوائي'}")
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 بريميوم", callback_data="c:prem")
    kb.button(text="💬 تعليق", callback_data="c:com")
    kb.button(text="🤖 تفعيل بوت", callback_data="c:bot")
    kb.button(text="🗳 تصويت", callback_data="c:vote")
    kb.button(text="📢 قناة اشتراك", callback_data="c:chan")
    if d["kind"] == "roulette":
        kb.button(text="🎟 فرص بالدعوة", callback_data="c:inv")
    kb.button(text="🚀 نشر الفعالية", callback_data="c:done")
    kb.button(text="❌ إلغاء", callback_data="c:cancel")
    kb.adjust(2, 2, 2, 1, 1)
    await target.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(StateFilter(New.cond, New.cond_text), F.data.startswith("c:"))
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
        if d.get("comment_chat") or d.get("comment_url"):   # الضغط مرة ثانية = إلغاء الشرط
            await state.update_data(comment_text=None, comment_chat=None, comment_url=None)
            await cb.answer("تم إلغاء شرط التعليق.")
            return await show_conditions(cb.message, state)
        await state.update_data(ask="comment_chat")
        await state.set_state(New.cond_text)
        kb = InlineKeyboardBuilder()
        kb.button(text="↩️ رجوع", callback_data="cg:back")
        kb.adjust(1)
        await cb.answer()
        return await cb.message.answer(
            "💬 <b>شرط التعليق</b>\n"
            "أرسل <b>رابط القناة</b> أو <b>رابط منشور منها</b> أو رابط الجروب:\n"
            "• https://t.me/اسم_القناة أو https://t.me/اسم_القناة/123\n"
            "• أو @يوزر / ايدي / منشور محوّل\n\n"
            "أي شكل مقبول، حتى لو البوت مو مشرف.\n"
            "💡 إذا كان البوت مشرفاً بمجموعة النقاش يُحتسب التعليق تلقائياً، وإلا يضغط المشارك «علّقت».",
            reply_markup=kb.as_markup())
    if act == "bot":
        await state.update_data(ask="bot")
        await state.set_state(New.cond_text)
        await cb.answer()
        return await cb.message.answer("🤖 أرسل رابط البوت المطلوب تفعيله (مثل https://t.me/SomeBot).\n"
                                       "ملاحظة: التفعيل يعتمد على ضغط المشارك زر «فعلتها» (لا يمكن التحقق منه تلقائياً).")
    if act == "vote":
        if d.get("vote_url"):   # الضغط مرة ثانية = إلغاء الشرط
            await state.update_data(vote_url=None)
            await cb.answer("تم إلغاء شرط التصويت.")
            return await show_conditions(cb.message, state)
        await state.update_data(ask="vote")
        await state.set_state(New.cond_text)
        await cb.answer()
        return await cb.message.answer("🗳 أرسل <b>رابط التعليق/المنشور</b> اللي يصوّت عليه المشارك "
                                       "(مثل https://t.me/channel/123).\n"
                                       "ملاحظة: التصويت ما يمكن التحقق منه تلقائياً، يعتمد على ضغط المشارك زر «صوّتت».")
    if act == "chan":
        await state.update_data(ask="chan")
        await state.set_state(New.cond_text)
        await cb.answer()
        return await cb.message.answer("📢 أرسل @يوزر القناة أو حوّل منشوراً منها (لازم تكون مشرفاً والبوت مشرف فيها).")
    if act == "done":
        await cb.answer()
        return await publish(cb, state, bot)


async def _bot_is_admin(bot, chat_id):
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
        return me.status == "administrator"
    except Exception:
        return False


async def resolve_group(m, bot):
    """يقبل رابط قناة / رابط منشور / رابط جروب / @يوزر / ايدي / منشور محوّل.
    يتأكد أن البوت مشرف بالقناة وبمجموعة التعليقات المرتبطة بها.
    يرجع (group_chat, رابط_علق_هنا, None) أو (None, None, رسالة_خطأ)"""
    chat, ref, post_link = None, None, None
    fo = getattr(m, "forward_origin", None)
    if fo is not None and getattr(fo, "chat", None) is not None:
        chat = fo.chat
        mid = getattr(fo, "message_id", None)
        if chat.username and mid:
            post_link = f"https://t.me/{chat.username}/{mid}"
    else:
        import re
        txt = (m.text or m.caption or "").strip()
        mm = re.search(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/([^\s]+)", txt, re.I) or \
            re.search(r"tg://resolve\?domain=([\w]+)(?:&post=(\d+))?", txt, re.I)
        if mm and "tg://" in mm.group(0).lower():
            txt = "https://t.me/" + mm.group(1) + (("/" + mm.group(2)) if mm.group(2) else "")
        elif mm:
            txt = "https://t.me/" + mm.group(1).rstrip(".,;:)»")
        else:
            u = re.search(r"@([A-Za-z][\w]{3,})", txt)
            i = re.search(r"(?<![\w/])-?\d{6,}(?![\w])", txt)
            txt = ("@" + u.group(1)) if u else (i.group(0) if i else txt)
        if txt.startswith(("https://t.me/", "http://t.me/", "t.me/")):
            path = txt.split("t.me/", 1)[1].split("?")[0].strip("/")
            parts = path.split("/")
            if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
                ref = int("-100" + parts[1])
                if len(parts) >= 3 and parts[-1].isdigit():
                    post_link = "https://t.me/" + path
            elif parts[0].startswith("+") or parts[0] == "joinchat":
                return None, None, ("❌ روابط الدعوة الخاصة ما أقدر أقرأها. أرسل رابط منشور من القناة أو يوزرها أو ايديها.")
            elif parts[0]:
                ref = "@" + parts[0]
                if len(parts) >= 2 and parts[-1].isdigit():
                    post_link = "https://t.me/" + path
        elif txt.startswith("@") or txt.lstrip("-").isdigit():
            ref = int(txt) if txt.lstrip("-").isdigit() else txt
        if ref is None:
            return None, None, "أرسل رابط القناة أو رابط منشور منها أو رابط الجروب."
        try:
            chat = await bot.get_chat(ref)
        except Exception:
            chat = None   # نقبلها حتى لو ما قدر البوت يوصلها
            if isinstance(ref, str) and not post_link:
                post_link = "https://t.me/" + ref[1:]
    # نقبل القناة/الجروب بأي حال، وإن كان البوت مشرفاً بمجموعة التعليقات يُحتسب التعليق تلقائياً
    channel, group = None, None
    if chat is not None:
        if chat.type == "channel":
            channel = chat
            try:
                linked = (await bot.get_chat(chat.id)).linked_chat_id
                group = await bot.get_chat(linked) if linked else None
            except Exception:
                group = None
        elif chat.type in ("group", "supergroup"):
            group = chat
    auto = bool(group) and await _bot_is_admin(bot, group.id)
    url = post_link
    if not url and (channel or group):
        url = await channel_url(bot, channel or group)
    if not url:
        return None, None, "❌ ما قدرت أحصل على رابط من هذا. أرسل رابط القناة أو منشور منها أو @يوزرها."
    return (group if auto else None), url, None


async def set_comment_group(target, state, bot, chat, url=None):
    """chat = مجموعة التعليق إن كان البوت مشرفاً فيها (احتساب تلقائي)، وإلا None (تأكيد بزر «علّقت»)."""
    await state.update_data(comment_chat=chat.id if chat else None, comment_url=url,
                            comment_text=None if chat else "confirm", ask=None)
    await state.set_state(New.cond)
    if chat:
        await target.answer(f"✅ تم اعتماد التعليق على «{esc(chat.title or '')}» (البوت مشرف، يُحتسب تلقائياً).\n"
                            "سيظهر شرط التعليق بالفعالية بكلمة «علق هنا» كرابط.")
    else:
        await target.answer("✅ تم اعتماد شرط التعليق.\n"
                            "البوت ليس مشرفاً بمجموعة التعليق، لذلك يضغط المشارك زر «علّقت» للتأكيد. "
                            "(إن جعلت البوت مشرفاً فيها يُحتسب التعليق تلقائياً.)")
    await show_conditions(target, state)


@router.callback_query(StateFilter(New.cond_text), F.data == "cg:back")
async def cg_back(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await show_conditions(cb.message, state)


@router.message(StateFilter(New.cond_text), F.text | F.forward_origin)
async def in_cond_text(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    ask = d.get("ask")
    if ask == "comment_chat":
        chat, url, err = await resolve_group(m, bot)
        if err:
            return await m.answer(err)
        return await set_comment_group(m, state, bot, chat, url)
    if ask == "vote":
        url = (m.text or "").strip()
        if not url.startswith("https://t.me/"):
            return await m.answer("أرسل رابط يبدأ بـ https://t.me/")
        await state.update_data(vote_url=url)
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
    if not d.get("channel"):
        return await cb.message.answer("❌ لازم تختار قناة للنشر.")
    if not await _bot_is_admin(bot, d["channel"]):
        return await cb.message.answer("❌ البوت ليس مشرفاً في قناة النشر. اجعله مشرفاً فيها ثم انشر مرة ثانية.")
    eid = await db.insert(
        "INSERT INTO events(owner_id,kind,title,prize,winners,channel_id,created_at,end_at,need_premium,"
        "comment_chat,comment_text,need_bot_url,invite_bonus,promo_text,min_members,max_channels,comment_url,vote_url,draw_mode,approve_mode,vote_goal,join_goal) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        uid, d["kind"], d["title"], d.get("prize"), d["winners"], d.get("channel"), now(),
        now() + int(d["hours"] * 3600), int(bool(d.get("premium"))), d.get("comment_chat"),
        d.get("comment_text"), d.get("bot_url"), int(d.get("invite", 0) or 0), d.get("promo"),
        int(d.get("min_members", 0)), int(ctx.db.get("chroulette_max_channels")), d.get("comment_url"), d.get("vote_url"),
        (d.get("draw_mode") or "auto") if ctx.is_owner(uid) else "auto", int(d.get("approve", 0)), (int(d.get("goal", 0) or 0) if d["kind"] == "votes" else 0), (int(d.get("goal", 0) or 0) if d["kind"] != "votes" else 0))
    for ch in d.get("extra", []):
        await db.run("INSERT OR IGNORE INTO event_channels(event_id,chat_id,title,username,url) VALUES(?,?,?,?,?)",
                     eid, ch["chat_id"], ch["title"], ch["username"], ch["url"])
    link = f"https://t.me/{ctx.bot_username}?start=ev_{eid}"
    if d.get("channel"):
        ev = await db.one("SELECT * FROM events WHERE id=?", eid)
        try:
            msg = await bot.send_message(d["channel"], await post_text(db, ev), reply_markup=post_markup(ev))
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
    await cb.message.answer(f"✅ تم إنشاء الفعالية #{eid} ونشرها بالقناة.", reply_markup=kb.as_markup())


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
    if not ev:
        await cb.answer("غير مسموح", show_alert=True)
        return None
    uid = cb.from_user.id
    if ev["owner_id"] != uid and not ctx.is_admin(uid) and not await ctx.db.one(
            "SELECT 1 FROM event_managers WHERE event_id=? AND user_id=?", ev["id"], uid):
        await cb.answer("غير مسموح", show_alert=True)
        return None
    return ev


@router.callback_query(F.data.startswith("evm:"))
async def ev_manage(cb: CallbackQuery):
    ev = await owned_event(cb, int(cb.data[4:]))
    if not ev:
        return
    await manage_view(cb, ev)


async def manage_view(cb, ev):
    db = ctx.db
    cnt = await db.val("SELECT COUNT(*) FROM participants WHERE event_id=?", ev["id"]) if ev["kind"] != "votes" \
        else await db.val("SELECT COUNT(*) FROM candidates WHERE approved=1 AND event_id=?", ev["id"])
    left = max(0, ev["end_at"] - now())
    txt = (f"⚙️ <b>{esc(ev['title'])}</b> (#{ev['id']})\nالحالة: {ev['status']}\n"
           + (f"🎯 الفائز: {'يدوي' if is_manual(ev) else 'عشوائي'}\n" if ctx.is_owner(cb.from_user.id) else "") +
           f"{'👥 المشاركون' if ev['kind'] != 'votes' else '🙋 المتسابقون'}: {cnt}\n"
           f"⏳ المتبقي: " + ("♾ مفتوحة" if left > 86400 * 365 * 5 else f"{left // 3600} ساعة {left % 3600 // 60} دقيقة"))
    kb = InlineKeyboardBuilder()
    if ev["status"] == "active":
        kb.button(text="🎡 تدوير العجلة (سحب تلقائي الآن)" if ev["kind"] != "votes" else "🏁 إنهاء الآن (الأعلى أصواتاً)",
                  callback_data=f"evend:{ev['id']}")
        if ctx.is_owner(cb.from_user.id):
            kb.button(text="🎯 اختيار الفائز يدوياً", callback_data=f"evpick:{ev['id']}")
        kb.button(text="⏱ +1 ساعة", callback_data=f"evext:{ev['id']}:1")
        kb.button(text="⏱ +24 ساعة", callback_data=f"evext:{ev['id']}:24")
        kb.button(text="🤝 مشاركة/نقل الصلاحيات", callback_data=f"evshr:{ev['id']}")
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


PICK_PAGE = 8


async def pick_candidates(ev):
    if ev["kind"] == "votes":
        rows = await ctx.db.all(
            "SELECT c.user_id AS uid, c.name AS name, COUNT(v.voter_id) AS n FROM candidates c "
            "LEFT JOIN votes v ON v.candidate_id=c.id WHERE c.approved=1 AND c.event_id=? GROUP BY c.id ORDER BY n DESC, c.created_at",
            ev["id"])
        return [(r["uid"], f"{r['name']} ({r['n']} صوت)") for r in rows]
    rows = await ctx.db.all(
        "SELECT p.user_id AS uid, u.first_name, u.username FROM participants p LEFT JOIN users u ON u.user_id=p.user_id "
        "WHERE p.event_id=? ORDER BY p.joined_at", ev["id"])
    return [(r["uid"], (r["first_name"] or ("@" + r["username"] if r["username"] else str(r["uid"])))) for r in rows]


async def pick_render(cb, state):
    d = await state.get_data()
    ev = await ctx.db.one("SELECT * FROM events WHERE id=?", d["ev_id"])
    people = await pick_candidates(ev)
    sel, page = d.get("sel", []), d.get("page", 0)
    pages = max(1, (len(people) + PICK_PAGE - 1) // PICK_PAGE)
    page = min(page, pages - 1)
    kb = InlineKeyboardBuilder()
    for uid, name in people[page * PICK_PAGE:(page + 1) * PICK_PAGE]:
        kb.button(text=f"{'✅' if uid in sel else '⬜'} {name[:40]}", callback_data=f"pk:t:{uid}")
    kb.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=f"pk:p:{page - 1}")
    nav.button(text=f"{page + 1}/{pages}", callback_data="pk:n")
    if page < pages - 1:
        nav.button(text="▶️", callback_data=f"pk:p:{page + 1}")
    nav.adjust(3)
    kb.attach(nav)
    kb.row(InlineKeyboardButton(text=f"🏁 اعتماد الفائزين ({len(sel)}/{ev['winners']})", callback_data="pk:ok"))
    kb.row(InlineKeyboardButton(text="❌ إلغاء", callback_data="pk:x"))
    txt = (f"🎯 <b>اختيار الفائز يدوياً</b> — {esc(ev['title'])}\n"
           f"اختر حتى {ev['winners']} فائز من {len(people)} "
           f"{'متسابق' if ev['kind'] == 'votes' else 'مشارك'}، ثم اضغط «اعتماد».")
    await show(cb, txt, kb.as_markup())


@router.callback_query(F.data.startswith("evpick:"))
async def ev_pick(cb: CallbackQuery, state: FSMContext):
    if not ctx.is_owner(cb.from_user.id):
        return await cb.answer("غير مسموح", show_alert=True)
    ev = await owned_event(cb, int(cb.data[7:]))
    if not ev:
        return
    if ev["status"] != "active":
        return await cb.answer("الفعالية منتهية.", show_alert=True)
    if not await pick_candidates(ev):
        return await cb.answer("لا يوجد مشاركون لاختيار فائز منهم.", show_alert=True)
    await state.clear()
    await state.set_state(Pick.sel)
    await state.update_data(ev_id=ev["id"], sel=[], page=0)
    await cb.answer()
    await pick_render(cb, state)


@router.callback_query(StateFilter(Pick.sel), F.data.startswith("pk:"))
async def pick_action(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not ctx.is_owner(cb.from_user.id):
        await state.clear()
        return await cb.answer("غير مسموح", show_alert=True)
    d = await state.get_data()
    ev = await owned_event(cb, d.get("ev_id", 0))
    if not ev:
        return await state.clear()
    parts = cb.data.split(":")
    act = parts[1]
    sel = list(d.get("sel", []))
    if act == "n":
        return await cb.answer()
    if act == "x":
        await state.clear()
        return await manage_view(cb, ev)
    if act == "p":
        await state.update_data(page=int(parts[2]))
    elif act == "t":
        uid = int(parts[2])
        if uid in sel:
            sel.remove(uid)
        elif len(sel) >= ev["winners"]:
            return await cb.answer(f"الحد الأقصى {ev['winners']} فائز.", show_alert=True)
        else:
            sel.append(uid)
        await state.update_data(sel=sel)
    elif act == "ok":
        valid = {u for u, _ in await pick_candidates(ev)}
        sel = [u for u in sel if u in valid]
        if not sel:
            return await cb.answer("اختر فائزاً واحداً على الأقل.", show_alert=True)
        await state.clear()
        await cb.answer("جاري الإعلان...")
        from services import finish_event
        res = await finish_event(bot, ctx.db, ev["id"], manual=sel)
        await audit(ctx.db, cb.from_user.id, "event_manual_winners", f"{ev['id']}:{sel}")
        if res is None:
            await cb.message.answer("⚠️ الفعالية انتهت مسبقاً.")
        return
    await cb.answer()
    await pick_render(cb, state)


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


async def register_channel(uid, chat):
    """يسجّل القناة للمالك. يرجع None عند النجاح أو رسالة خطأ."""
    db = ctx.db
    row = await db.one("SELECT owner_id FROM channels WHERE chat_id=?", chat.id)
    if row and row["owner_id"] != uid:
        return "⚠️ هذي القناة مسجلة بحساب آخر. تواصل مع المالك إذا هي قناتك."
    await db.run("INSERT INTO channels(chat_id,owner_id,title,username,active,added_at) VALUES(?,?,?,?,1,?) "
                 "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username, active=1",
                 chat.id, uid, chat.title, chat.username, now())
    await ensure_channel_link(db, uid, chat.id, chat.title)


@router.message(StateFilter(AddCh.wait), F.text | F.forward_origin)
async def ch_add_msg(m: Message, state: FSMContext, bot: Bot):
    db = ctx.db
    chat, err = await resolve_channel(m, bot)
    if err:
        return await m.answer(err)
    err = await register_channel(m.from_user.id, chat)
    if err:
        return await m.answer(err)
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
    try:   # مشرفو/مالكو قناة الروليت نفسها ممنوعون (ما عدا مالك/مشرفي البوت)
        m = await bot.get_chat_member(ev["channel_id"], uid)
        if m.status in ("administrator", "creator") and not ctx.is_admin(uid):
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
    rem = await db.one("SELECT user_id FROM entry_removals WHERE event_id=? AND chat_id=?", ev["id"], chat.id)
    if rem and rem["user_id"] == uid:   # حُذفت رسالتها سابقاً: إعادة إرسال القناة = إرجاع
        await state.clear()
        ok, txt = await restore_entry(bot, db, ev["id"], chat.id, uid)
        return await m.answer(txt)
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
    try:   # ينشر الرسالة التي حددها صاحب الروليت بقناة المشارك
        msg = await bot.send_message(chat.id, promo_text(ev), reply_markup=promo_markup(ev["id"]))
    except Exception:
        return await m.answer("❌ تعذر النشر بقناتك. تأكد أن البوت مشرف وله صلاحية نشر الرسائل.")
    await db.run("INSERT OR IGNORE INTO event_entries(event_id,chat_id,user_id,title,members,message_id,created_at) "
                 "VALUES(?,?,?,?,?,?,?)", ev["id"], chat.id, uid, chat.title, members, msg.message_id, now())
    await db.run("INSERT OR IGNORE INTO participants(event_id,user_id,joined_at) VALUES(?,?,?)", ev["id"], uid, now())
    schedule_refresh(bot, db, ev["id"])
    await check_join_goal(bot, db, ev["id"])
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
    gone = await ctx.db.all("SELECT chat_id, title FROM entry_removals WHERE event_id=? AND user_id=?", ev_id, cb.from_user.id)
    txt = f"🎟 فرصك: {len(rows)}\n" + "\n".join(f"• {esc(r['title'] or '')} ({r['members']})" for r in rows) if rows else "ما سجّلت أي قناة بعد."
    kb = InlineKeyboardBuilder()
    if gone:
        txt += "\n\n⚠️ قنوات حُذفت رسالتها (أُزيلت من المسابقة):\n" + "\n".join(f"• {esc(g['title'] or '')}" for g in gone)
        for g in gone:
            kb.button(text=f"↩️ إرجاع «{(g['title'] or '')[:25]}»", callback_data=f"cret:{ev_id}:{g['chat_id']}")
        kb.adjust(1)
    await cb.message.answer(txt, reply_markup=kb.as_markup() if gone else None)
    await cb.answer()


@router.callback_query(F.data.startswith("cret:"))
async def cret(cb: CallbackQuery, bot: Bot):
    """زر «إرجاع» بعد حذف رسالة الفعالية من القناة."""
    try:
        _, ev_id, chat_id = cb.data.split(":")
        ev_id, chat_id = int(ev_id), int(chat_id)
    except ValueError:
        return await cb.answer()
    ok, txt = await restore_entry(bot, ctx.db, ev_id, chat_id, cb.from_user.id)
    await cb.answer(txt, show_alert=not ok)
    if ok:
        try:
            await cb.message.edit_text(f"{txt}\n⚠️ لا تحذف الرسالة مرة ثانية وإلا أُزيلت مشاركتك.", reply_markup=None)
        except Exception:
            pass


# ---- مشاركة / نقل صلاحيات الفعالية ----
@router.callback_query(F.data.startswith("evshr:"))
async def ev_share_help(cb: CallbackQuery):
    ev = await owned_event(cb, int(cb.data[6:]))
    if not ev:
        return
    rows = await ctx.db.all("SELECT user_id FROM event_managers WHERE event_id=?", ev["id"])
    ids = ", ".join(str(r["user_id"]) for r in rows) or "—"
    await cb.message.answer(
        f"🤝 <b>صلاحيات الفعالية #{ev['id']}</b>\nالمشاركون بالإدارة: {ids}\n\n"
        f"/evshare {ev['id']} USER_ID — مشاركة الصلاحيات\n"
        f"/evunshare {ev['id']} USER_ID — إزالة مشارك\n"
        f"/evtransfer {ev['id']} USER_ID — نقل الملكية (للمالك فقط)\n\n"
        "ملاحظة: يجب أن يكون الشخص قد فتح البوت مسبقاً.")
    await cb.answer()


async def _share_cmd(m: Message, command: CommandObject, mode: str):
    try:
        eid, target = (int(x) for x in (command.args or "").split())
    except ValueError:
        return await m.answer("الصيغة: /" + mode + " رقم_الفعالية رقم_المستخدم")
    ev = await ctx.db.one("SELECT * FROM events WHERE id=?", eid)
    uid = m.from_user.id
    is_owner = bool(ev) and (ev["owner_id"] == uid or ctx.is_admin(uid))
    is_mgr = bool(ev) and bool(await ctx.db.one(
        "SELECT 1 FROM event_managers WHERE event_id=? AND user_id=?", eid, uid))
    if not ev or not (is_owner or (is_mgr and mode == "evshare")):
        return await m.answer("غير مسموح")
    if not await ctx.db.one("SELECT 1 FROM users WHERE user_id=?", target):
        return await m.answer("هذا المستخدم لم يفتح البوت بعد.")
    if mode == "evshare":
        await ctx.db.run("INSERT OR IGNORE INTO event_managers(event_id,user_id) VALUES(?,?)", eid, target)
        await m.answer("✅ تمت مشاركة الصلاحيات.")
    elif mode == "evunshare":
        await ctx.db.run("DELETE FROM event_managers WHERE event_id=? AND user_id=?", eid, target)
        await m.answer("✅ أُزيل من الإدارة.")
    else:
        await ctx.db.run("UPDATE events SET owner_id=? WHERE id=?", target, eid)
        await ctx.db.run("DELETE FROM event_managers WHERE event_id=? AND user_id=?", eid, target)
        await m.answer("✅ تم نقل ملكية الفعالية.")
    await audit(ctx.db, uid, mode, f"{eid}:{target}")


@router.message(Command("evshare"))
async def cmd_evshare(m: Message, command: CommandObject):
    await _share_cmd(m, command, "evshare")


@router.message(Command("evunshare"))
async def cmd_evunshare(m: Message, command: CommandObject):
    await _share_cmd(m, command, "evunshare")


@router.message(Command("evtransfer"))
async def cmd_evtransfer(m: Message, command: CommandObject):
    await _share_cmd(m, command, "evtransfer")


@router.callback_query(F.data.regexp(r"^capp:(ok|no):\d+$"))
async def cand_decide(cb: CallbackQuery, bot: Bot):
    _, act, cid = cb.data.split(":")
    row = await ctx.db.one("SELECT * FROM candidates WHERE id=?", int(cid))
    ev = await ctx.db.one("SELECT * FROM events WHERE id=?", row["event_id"]) if row else None
    if not ev:
        return await cb.answer("غير موجود", show_alert=True)
    uid = cb.from_user.id
    if ev["owner_id"] != uid and not ctx.is_admin(uid) and not await ctx.db.one(
            "SELECT 1 FROM event_managers WHERE event_id=? AND user_id=?", ev["id"], uid):
        return await cb.answer("غير مسموح", show_alert=True)
    if act == "ok":
        await ctx.db.run("UPDATE candidates SET approved=1 WHERE id=?", row["id"])
        link = f"https://t.me/{ctx.bot_username}?start=v_{ev['id']}_{row['id']}"
        msg = f"✅ تم قبولك في «{esc(ev['title'])}»\nرابطك لجلب الأصوات:\n{link}"
    else:
        await ctx.db.run("DELETE FROM candidates WHERE id=? AND approved=0", row["id"])
        msg = f"❌ لم يتم قبول مشاركتك في «{esc(ev['title'])}»."
    try:
        await bot.send_message(row["user_id"], msg)
    except Exception:
        pass
    await audit(ctx.db, uid, "cand_" + act, f"{ev['id']}:{row['user_id']}")
    await refresh_post(bot, ctx.db, ev["id"], force=True)
    await cb.message.edit_text(cb.message.text + ("\n\n✅ تم القبول" if act == "ok" else "\n\n❌ تم الرفض"))
    await cb.answer()


@router.callback_query(F.data.startswith("chwin:"))
async def ch_win(cb: CallbackQuery, bot: Bot):
    """المنظّم/المشرف يسحب الفائز من داخل قناة المسابقة."""
    from services import finish_event
    ev = await ctx.db.one("SELECT * FROM events WHERE id=?", int(cb.data[6:]))
    uid = cb.from_user.id
    if not ev or ev["status"] != "active":
        return await cb.answer("الفعالية منتهية.", show_alert=True)
    if ev["owner_id"] != uid and not ctx.is_admin(uid) and not await ctx.db.one(
            "SELECT 1 FROM event_managers WHERE event_id=? AND user_id=?", ev["id"], uid):
        return await cb.answer("🚫 للمنظّم فقط.", show_alert=True)
    if is_manual(ev):   # فعالية يدوية (مالك البوت فقط)
        if not ctx.is_owner(uid):
            return await cb.answer("🚫 للمنظّم فقط.", show_alert=True)
        kb = InlineKeyboardBuilder()
        kb.button(text="🎯 اختيار الفائز", callback_data=f"evpick:{ev['id']}")
        kb.button(text="🎡 سحب عشوائي الآن", callback_data=f"evend:{ev['id']}")
        kb.button(text="⚙️ إدارة الفعالية", callback_data=f"evm:{ev['id']}")
        kb.adjust(1)
        try:
            await bot.send_message(uid, f"👑 «{esc(ev['title'])}»: اختر طريقة تحديد الفائز:", reply_markup=kb.as_markup())
            return await cb.answer("أرسلت لك خيارات تحديد الفائز بالخاص.", show_alert=True)
        except Exception:
            return await cb.answer("افتح البوت بالخاص (/start) ثم أعد الضغط.", show_alert=True)
    await cb.answer("جاري السحب...")
    await audit(ctx.db, uid, "channel_draw", ev["id"])
    await finish_event(bot, ctx.db, ev["id"])
