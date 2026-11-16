import random
import time
from urllib.parse import quote
from html import escape as esc

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ctx
from config import OWNER_IDS
from i18n import t
from services import (KIND_ICON, check_spike, check_user, clear_gate, event_text, gate_missing, mention, notify_join,
                      now, owner_links, owner_quality, record_referral, resolve_source, retention_sample, safe_send,
                      top_channels, create_payout, ensure_default_link, audit)
from ui import gate_screen, home_kb, menu_kb, show

router = Router()
EMOJIS = list("🍎🚗🐶🌙⭐🔥🎈🍕⚽🎧🌹🐱🍉🚀🎲")
_last_vote = {}


async def lang_of(uid):
    return await ctx.db.val("SELECT lang FROM users WHERE user_id=?", uid) or "ar"


# ───────────── البداية والكابتشا ─────────────
@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, bot: Bot):
    if m.chat.type != "private":
        return
    db = ctx.db
    u = m.from_user
    payload = (command.args or "").strip()[:64]
    is_new = await db.touch_user(u)
    if is_new and payload:   # أول تفعيل للبوت فقط: يُحتسب لصاحب الرابط ولا يتغير بعدها
        src = await resolve_source(db, payload)
        if src:
            await record_referral(db, u.id, src)
    await db.run("UPDATE users SET payload=? WHERE user_id=?", payload, u.id)
    await gate(m, u.id, bot)


async def gate(target, uid, bot):
    db = ctx.db
    user = await db.one("SELECT * FROM users WHERE user_id=?", uid)
    lang = user["lang"]
    if user["banned"]:
        return await target.answer(t(lang, "banned"))
    if not ctx.is_admin(uid):
        if db.get("maintenance") == "1":
            return await target.answer(t(lang, "maint"))
        missing = await gate_missing(bot, db, uid)   # الاشتراك الإجباري العام
        if missing:
            return await gate_screen(target, lang, missing)
    if not user["verified"]:
        if db.get("captcha_on") == "1":
            return await send_captcha(target, uid, lang)
        await db.run("UPDATE users SET verified=1 WHERE user_id=?", uid)
    payload = user["payload"] or ""
    await db.run("UPDATE users SET payload='' WHERE user_id=?", uid)
    await route(target, uid, lang, payload)


async def send_captcha(target, uid, lang):
    choices = random.sample(EMOJIS, 6)
    answer = random.choice(choices)
    await ctx.db.run("UPDATE users SET captcha_answer=? WHERE user_id=?", answer, uid)
    kb = InlineKeyboardBuilder()
    for e in choices:
        kb.button(text=e, callback_data=f"cap:{e}")
    kb.adjust(3)
    await target.answer(t(lang, "captcha", e=answer), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("cap:"))
async def cap(cb: CallbackQuery, bot: Bot):
    db = ctx.db
    uid = cb.from_user.id
    user = await db.one("SELECT * FROM users WHERE user_id=?", uid)
    if not user:
        return await cb.answer()
    if user["verified"]:
        return await cb.answer("✅")
    if cb.data[4:] == user["captcha_answer"]:
        await db.run("UPDATE users SET verified=1, captcha_answer=NULL WHERE user_id=?", uid)
        await cb.answer(t(user["lang"], "captcha_ok"))
        await gate(cb.message, uid, bot)
    else:
        await cb.answer(t(user["lang"], "captcha_bad"), show_alert=True)
        await send_captcha(cb.message, uid, user["lang"])


async def route(target, uid, lang, payload):
    try:
        if payload.startswith("ev_"):
            parts = payload.split("_")
            eid = int(parts[1])
            if len(parts) > 2 and parts[2].isdigit() and int(parts[2]) != uid:   # دعوة صديق = فرصة إضافية
                await ctx.db.run("INSERT OR IGNORE INTO event_invites(event_id,user_id,inviter_id) VALUES(?,?,?)",
                                 eid, uid, int(parts[2]))
            return await show_event(target, lang, eid)
        if payload.startswith("v_"):
            _, e, c = payload.split("_")
            return await show_candidate(target, lang, int(e), int(c))
    except ValueError:
        pass
    await target.answer(t(lang, "welcome"), reply_markup=menu_kb(lang, ctx.is_admin(uid)))


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    lang = await lang_of(m.from_user.id)
    await m.answer(t(lang, "welcome"), reply_markup=menu_kb(lang, ctx.is_admin(m.from_user.id)))


@router.message(Command("menu"))
async def cmd_menu(m: Message):
    lang = await lang_of(m.from_user.id)
    await m.answer(t(lang, "welcome"), reply_markup=menu_kb(lang, ctx.is_admin(m.from_user.id)))


# ───────────── القائمة الرئيسية ─────────────
@router.callback_query(F.data == "m:home")
async def m_home(cb: CallbackQuery):
    lang = await lang_of(cb.from_user.id)
    await show(cb, t(lang, "welcome"), menu_kb(lang, ctx.is_admin(cb.from_user.id)))
    await cb.answer()


@router.callback_query(F.data == "m:rules")
async def m_rules(cb: CallbackQuery):
    db = ctx.db
    lang = await lang_of(cb.from_user.id)
    txt = t(lang, "rules", days=db.get("hold_days"), reward=db.get("ref_reward"), thr=db.get("ref_threshold"))
    await show(cb, txt, home_kb(lang))
    await cb.answer()


@router.callback_query(F.data == "m:lang")
async def m_lang(cb: CallbackQuery):
    lang = await lang_of(cb.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="العربية", callback_data="lang:ar")
    kb.button(text="English", callback_data="lang:en")
    await show(cb, t(lang, "pick_lang"), kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("lang:"))
async def set_lang(cb: CallbackQuery):
    lang = cb.data[5:] if cb.data[5:] in ("ar", "en") else "ar"
    await ctx.db.run("UPDATE users SET lang=? WHERE user_id=?", lang, cb.from_user.id)
    await show(cb, t(lang, "lang_set") + "\n\n" + t(lang, "welcome"), menu_kb(lang, ctx.is_admin(cb.from_user.id)))
    await cb.answer()


# ───────────── صفحة الفعالية والمشاركة ─────────────
async def show_event(target, lang, ev_id):
    db = ctx.db
    ev = await db.one("SELECT * FROM events WHERE id=?", ev_id)
    if not ev:
        return await target.answer(t(lang, "ev_none"))
    text = await event_text(db, ev)
    kb = InlineKeyboardBuilder()
    if ev["kind"] in ("roulette", "chroulette"):
        n = await db.val("SELECT COUNT(*) FROM participants WHERE event_id=?", ev_id)
        text += f"\n\n👥 المشاركون: {n}"
        if ev["status"] == "active":
            if ev["kind"] == "chroulette":
                kb.button(text="✅ شارك بقناتي", callback_data=f"cadd:{ev_id}")
                kb.button(text="📋 قنواتي المشاركة", callback_data=f"cmine:{ev_id}")
            else:
                kb.button(text=t(lang, "btn_join"), callback_data=f"join:{ev_id}")
    else:
        rows = await db.all(
            "SELECT c.id,c.name,COUNT(v.voter_id) AS votes FROM candidates c LEFT JOIN votes v ON v.candidate_id=c.id "
            "WHERE c.event_id=? GROUP BY c.id ORDER BY votes DESC, c.created_at LIMIT 15", ev_id)
        text += f"\n\n🙋 المتسابقون: {len(rows)}"
        if ev["status"] == "active":
            kb.button(text=t(lang, "btn_cand"), callback_data=f"cand:{ev_id}")
            for r in rows:
                kb.button(text=f"🗳 {r['name']} • {r['votes']}", callback_data=f"vote:{ev_id}:{r['id']}")
    if ev["status"] == "ended":
        ws = await db.all("SELECT u.user_id,u.first_name,u.username FROM winners w JOIN users u ON u.user_id=w.user_id "
                          "WHERE w.event_id=? ORDER BY w.place", ev_id)
        if ws:
            text += "\n\n🏆 الفائزون:\n" + "\n".join(f"{i}. {mention(w)}" for i, w in enumerate(ws, 1))
    if ev["status"] != "active":
        text += "\n\n" + t(lang, "ev_ended")
    kb.button(text="🚩", callback_data=f"rep:{ev_id}")
    kb.button(text=t(lang, "btn_home"), callback_data="m:home")
    kb.adjust(1)
    await target.answer(text, reply_markup=kb.as_markup())


async def show_candidate(target, lang, ev_id, cand_id):
    c = await ctx.db.one("SELECT c.*, (SELECT COUNT(*) FROM votes v WHERE v.candidate_id=c.id) AS votes "
                         "FROM candidates c WHERE c.id=? AND c.event_id=?", cand_id, ev_id)
    if not c:
        return await target.answer(t(lang, "ev_none"))
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🗳 {c['name']}", callback_data=f"vote:{ev_id}:{cand_id}")
    kb.button(text="📋", callback_data=f"ev:{ev_id}")
    kb.adjust(1)
    await target.answer(f"🏆 <b>{esc(c['name'])}</b>\n🗳 {c['votes']}", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("ev:") & ~F.data.startswith("ev:menu"))
async def open_event(cb: CallbackQuery):
    try:
        await show_event(cb.message, await lang_of(cb.from_user.id), int(cb.data[3:]))
    except ValueError:
        pass
    await cb.answer()


async def fails_screen(cb, ev, fails, retry_cb):
    lang = await lang_of(cb.from_user.id)
    lines, kb = [t(lang, "fail_title")], InlineKeyboardBuilder()
    for code, data in fails:
        if code == "captcha":
            lines.append(t(lang, "f_captcha"))
        elif code == "channel":
            lines.append(t(lang, "f_channel", title=esc(data.get("title") or "")))
            if data.get("url"):
                kb.button(text=f"📢 {data.get('title') or ''}", url=data["url"])
        elif code == "premium":
            lines.append(t(lang, "f_premium"))
        elif code == "comment":
            lines.append(t(lang, "f_comment", text=esc(data)))
        elif code == "botconfirm":
            lines.append(t(lang, "f_bot"))
            kb.button(text="🤖", url=data)
            kb.button(text=t(lang, "btn_confirm"), callback_data=f"conf:{ev['id']}")
        elif code == "username":
            lines.append(t(lang, "f_username"))
        elif code == "photo":
            lines.append(t(lang, "f_photo"))
        elif code == "banned":
            lines.append(t(lang, "banned"))
    kb.button(text=t(lang, "btn_retry"), callback_data=retry_cb)
    kb.adjust(1)
    await cb.message.answer("\n".join(lines), reply_markup=kb.as_markup())
    await cb.answer()


async def load_active(cb, ev_id):
    ev = await ctx.db.one("SELECT * FROM events WHERE id=?", ev_id)
    lang = await lang_of(cb.from_user.id)
    if not ev:
        await cb.answer(t(lang, "ev_none"), show_alert=True)
        return None, lang
    if ev["status"] != "active" or ev["end_at"] <= now():
        await cb.answer(t(lang, "ev_ended"), show_alert=True)
        return None, lang
    return ev, lang


@router.callback_query(F.data.startswith("join:"))
async def join(cb: CallbackQuery, bot: Bot):
    db = ctx.db
    await db.touch_user(cb.from_user)
    ev_id = int(cb.data[5:])
    ev, lang = await load_active(cb, ev_id)
    if not ev:
        return
    if ev["kind"] != "roulette":
        return await cb.answer("استخدم زر «شارك بقناتي».", show_alert=True)
    fails = await check_user(bot, db, ev, cb.from_user.id)
    if fails:
        return await fails_screen(cb, ev, fails, cb.data)
    n = await db.run("INSERT OR IGNORE INTO participants(event_id,user_id,joined_at) VALUES(?,?,?)",
                     ev_id, cb.from_user.id, now())
    await cb.answer(t(lang, "joined" if n else "already"), show_alert=True)
    if n:
        await notify_join(bot, db, ev, cb.from_user.id)
    if ev["invite_bonus"]:
        link = f"https://t.me/{ctx.bot_username}?start=ev_{ev_id}_{cb.from_user.id}"
        cnt = await db.val("SELECT COUNT(*) FROM event_invites i JOIN participants p ON p.event_id=i.event_id "
                           "AND p.user_id=i.user_id WHERE i.event_id=? AND i.inviter_id=?", ev_id, cb.from_user.id)
        await cb.message.answer(f"🎟 ارفع فرصك! كل صديق يشارك عبر رابطك يعطيك فرصة إضافية (حتى {ev['invite_bonus']}).\n"
                                f"🔗 {link}\n\nفرصك الآن: {1 + min(cnt, ev['invite_bonus'])}")


@router.callback_query(F.data.startswith("conf:"))
async def conf(cb: CallbackQuery):
    ev_id = int(cb.data[5:])
    await ctx.db.run("INSERT OR IGNORE INTO confirms(event_id,user_id) VALUES(?,?)", ev_id, cb.from_user.id)
    await cb.answer("✅", show_alert=False)


@router.callback_query(F.data.startswith("cand:"))
async def cand(cb: CallbackQuery, bot: Bot):
    db = ctx.db
    await db.touch_user(cb.from_user)
    ev_id = int(cb.data[5:])
    ev, lang = await load_active(cb, ev_id)
    if not ev:
        return
    fails = await check_user(bot, db, ev, cb.from_user.id)
    if fails:
        return await fails_screen(cb, ev, fails, cb.data)
    u = cb.from_user
    name = f"@{u.username}" if u.username else (u.first_name or str(u.id))
    await db.run("INSERT OR IGNORE INTO candidates(event_id,user_id,name,created_at) VALUES(?,?,?,?)",
                 ev_id, u.id, name, now())
    cid = await db.val("SELECT id FROM candidates WHERE event_id=? AND user_id=?", ev_id, u.id)
    link = f"https://t.me/{ctx.bot_username}?start=v_{ev_id}_{cid}"
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 ترتيبي", callback_data=f"myrank:{ev_id}")
    kb.button(text="📤 شارك رابطي", url="https://t.me/share/url?url=" + quote(link))
    kb.adjust(1)
    await cb.message.answer(t(lang, "cand_ok", link=link), reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("vote:"))
async def vote(cb: CallbackQuery, bot: Bot):
    db = ctx.db
    uid = cb.from_user.id
    if time.time() - _last_vote.get(uid, 0) < 1:
        return await cb.answer(t(await lang_of(uid), "slow"))
    _last_vote[uid] = time.time()
    await db.touch_user(cb.from_user)
    _, e, c = cb.data.split(":")
    ev, lang = await load_active(cb, int(e))
    if not ev or ev["kind"] != "votes":
        return
    age = now() - (await db.val("SELECT created_at FROM users WHERE user_id=?", uid) or 0)
    need = int(float(db.get("vote_min_age")) * 60)
    if age < need and not ctx.is_admin(uid):   # حسابات جديدة بالبوت لا تصوّت فوراً (ضد الرشق)
        return await cb.answer(t(lang, "vote_wait", m=(need - age) // 60 + 1), show_alert=True)
    cand_row = await db.one("SELECT * FROM candidates WHERE id=? AND event_id=?", int(c), ev["id"])
    if not cand_row:
        return await cb.answer(t(lang, "ev_none"), show_alert=True)
    if cand_row["user_id"] == uid:
        return await cb.answer(t(lang, "self_vote"), show_alert=True)
    fails = await check_user(bot, db, ev, uid)
    if fails:
        return await fails_screen(cb, ev, fails, cb.data)
    n = await db.run("INSERT OR IGNORE INTO votes(event_id,voter_id,candidate_id,created_at) VALUES(?,?,?,?)",
                     ev["id"], uid, cand_row["id"], now())
    if not n:
        return await cb.answer(t(lang, "voted_before"), show_alert=True)
    await cb.answer(t(lang, "voted", name=cand_row["name"]), show_alert=True)
    await check_spike(bot, db, ev, cand_row["id"])


@router.callback_query(F.data.startswith("rank:"))
async def rank(cb: CallbackQuery):
    ev_id = int(cb.data[5:])
    rows = await ctx.db.all(
        "SELECT c.name, COUNT(v.voter_id) AS votes FROM candidates c LEFT JOIN votes v ON v.candidate_id=c.id "
        "WHERE c.event_id=? GROUP BY c.id ORDER BY votes DESC, c.created_at LIMIT 5", ev_id)
    txt = "\n".join(f"{i}. {r['name']} — {r['votes']}" for i, r in enumerate(rows, 1)) or "—"
    await cb.answer(txt[:190], show_alert=True)


@router.callback_query(F.data.startswith("rep:"))
async def report(cb: CallbackQuery, bot: Bot):
    ev_id = int(cb.data[4:])
    await ctx.db.run("INSERT INTO reports(reporter_id,event_id,reason,created_at) VALUES(?,?,?,?)",
                     cb.from_user.id, ev_id, "report", now())
    for uid in ctx.mods | set(OWNER_IDS):
        await safe_send(bot, uid, f"🚩 بلاغ جديد على الفعالية #{ev_id}")
    await cb.answer(t(await lang_of(cb.from_user.id), "reported"), show_alert=True)


# ───────────── الأقسام والترتيب ─────────────
@router.callback_query(F.data == "m:dir")
async def m_dir(cb: CallbackQuery):
    lang = await lang_of(cb.from_user.id)
    if ctx.db.get("feat_directory") != "1":
        return await cb.answer(t(lang, "feat_off"), show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔴 الفعاليات الجارية الآن", callback_data="dir:live")
    kb.button(text="🏆 قنوات المسابقات", callback_data="dir:votes")
    kb.button(text="🎡 قنوات الروليت", callback_data="dir:roulette")
    kb.button(text="🌐 قنوات رولت القنوات", callback_data="dir:chroulette")
    kb.button(text="🏅 سجل الفائزين", callback_data="dir:wins")
    kb.button(text="🔥 الأكثر جلباً للأعضاء", callback_data="dir:top:all")
    kb.button(text="🥇 أفضل المنظّمين", callback_data="dir:lead")
    kb.button(text=t(lang, "btn_home"), callback_data="m:home")
    kb.adjust(1)
    await show(cb, "🌟 <b>الأقسام</b>", kb.as_markup())
    await cb.answer()


def channel_buttons(kb, rows):
    for r in rows:
        kb.button(text=r["title"] or "?", callback_data=f"dir:ch:{r['chat_id']}")


@router.callback_query(F.data.in_({"dir:votes", "dir:roulette", "dir:chroulette"}))
async def dir_kind(cb: CallbackQuery):
    kind = cb.data[4:]
    rows = await ctx.db.all(
        "SELECT c.chat_id, c.title, COUNT(*) AS n FROM events e JOIN channels c ON c.chat_id=e.channel_id "
        "WHERE e.kind=? AND e.status='active' AND c.active=1 GROUP BY c.chat_id ORDER BY n DESC LIMIT 20", kind)
    kb = InlineKeyboardBuilder()
    channel_buttons(kb, rows)
    kb.button(text="⬅️", callback_data="m:dir")
    kb.adjust(1)
    title = {"votes": "🏆 قنوات المسابقات النشطة", "roulette": "🎡 قنوات الروليت النشطة",
             "chroulette": "🌐 قنوات رولت القنوات النشطة"}[kind]
    await show(cb, title + ("" if rows else "\n\nلا توجد قنوات الآن."), kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("dir:top:"))
async def dir_top(cb: CallbackQuery):
    period = cb.data.split(":")[2]
    since = {"day": now() - 86400, "week": now() - 7 * 86400}.get(period, 0)
    rows = await top_channels(ctx.db, since)
    kb = InlineKeyboardBuilder()
    for label, p in (("اليوم", "day"), ("الأسبوع", "week"), ("الكل", "all")):
        kb.button(text=label, callback_data=f"dir:top:{p}")
    for r in rows:
        kb.button(text=f"{r['title']} • {r['brought']}", callback_data=f"dir:ch:{r['channel_id']}")
    kb.button(text="⬅️", callback_data="m:dir")
    kb.adjust(3, 1)
    await show(cb, "🔥 <b>الأكثر جلباً للأعضاء</b>" + ("" if rows else "\n\nلا بيانات بعد."), kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "dir:lead")
async def dir_lead(cb: CallbackQuery):
    rows = await ctx.db.all(
        "SELECT u.user_id,u.first_name,u.username, COUNT(*) AS n FROM referrals r JOIN users u ON u.user_id=r.owner_id "
        "WHERE r.status IN ('pending','valid') GROUP BY r.owner_id ORDER BY n DESC LIMIT 10")
    txt = "🥇 <b>أفضل المنظّمين</b>\n\n" + ("\n".join(f"{i}. {esc(r['first_name'] or r['username'] or str(r['user_id']))} — {r['n']}"
                                                      for i, r in enumerate(rows, 1)) or "لا بيانات بعد.")
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️", callback_data="m:dir")
    await show(cb, txt, kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("dir:ch:"))
async def dir_channel(cb: CallbackQuery):
    db = ctx.db
    chat_id = int(cb.data.split(":")[2])
    ch = await db.one("SELECT * FROM channels WHERE chat_id=?", chat_id)
    if not ch:
        return await cb.answer("—", show_alert=True)
    brought = await db.one(
        "SELECT SUM(CASE WHEN r.status IN ('pending','valid') THEN 1 ELSE 0 END) AS b, "
        "SUM(CASE WHEN r.status='valid' THEN 1 ELSE 0 END) AS v FROM referrals r "
        "JOIN invite_links l ON l.code=r.link_code WHERE l.channel_id=?", chat_id)
    active = await db.all("SELECT id,kind,title FROM events WHERE channel_id=? AND status='active' LIMIT 10", chat_id)
    ended = await db.val("SELECT COUNT(*) FROM events WHERE channel_id=? AND status='ended'", chat_id)
    txt = (f"📺 <b>{esc(ch['title'] or '')}</b>\n\n👥 جلبت للبوت: {brought['b'] or 0} (معتمد: {brought['v'] or 0})\n"
           f"🏁 فعاليات منتهية: {ended}\n\n🔴 النشطة الآن:" + ("" if active else " —"))
    kb = InlineKeyboardBuilder()
    for e in active:
        kb.button(text=f"{KIND_ICON.get(e['kind'], '')} {e['title']}", callback_data=f"ev:{e['id']}")
    if ch["username"]:
        kb.button(text="↗️ فتح القناة", url=f"https://t.me/{ch['username']}")
    kb.button(text="⬅️", callback_data="m:dir")
    kb.adjust(1)
    await show(cb, txt, kb.as_markup())
    await cb.answer()


# ───────────── روابطي ومكافآتي ─────────────
@router.callback_query(F.data == "m:links")
async def m_links(cb: CallbackQuery):
    db = ctx.db
    uid = cb.from_user.id
    lang = await lang_of(uid)
    await ensure_default_link(db, uid)
    links = await owner_links(db, uid)
    lines = ["🔗 <b>روابطك ومكافآتك</b>\n"]
    for l in links:
        name = "الرابط العام" if l["is_default"] else (l["ch_title"] or l["title"] or "قناة")
        url = f"https://t.me/{ctx.bot_username}?start=ref_{l['code']}"
        lines.append(f"<b>{esc(name)}</b>\n{url}\n"
                     f"✅ معتمد: {l['valid']}/{l['thr']} • ⏳ بالانتظار: {l['pending']} • 💵 {l['prc']}$ لكل {l['thr']}\n")
    lines.append("كل من يفعّل البوت لأول مرة عبر رابطك يُحتسب لك مرة وحدة فقط.")
    paid = await db.val("SELECT COALESCE(SUM(amount),0) FROM payouts WHERE owner_id=? AND status='paid'", uid)
    pend = await db.val("SELECT COALESCE(SUM(amount),0) FROM payouts WHERE owner_id=? AND status IN ('pending','approved')", uid)
    lines.append(f"\n💰 مدفوع: {paid}$ • قيد المراجعة: {pend}$")
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 اطلب الصرف", callback_data="pay:req")
    kb.button(text=t(lang, "btn_home"), callback_data="m:home")
    kb.adjust(1)
    await show(cb, "\n".join(lines), kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "pay:req")
async def pay_req(cb: CallbackQuery, bot: Bot):
    db = ctx.db
    res = await create_payout(db, cb.from_user.id)
    if res == "exists":
        return await cb.answer("لديك طلب صرف قيد المراجعة.", show_alert=True)
    if not res:
        return await cb.answer("لا يوجد رصيد مستحق بعد. أكمل العدد المطلوب أولاً.", show_alert=True)
    await cb.answer("✅ تم إرسال طلب الصرف للمراجعة", show_alert=True)
    owner = await db.one("SELECT user_id,username,first_name FROM users WHERE user_id=?", cb.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ موافقة", callback_data=f"pay:approve:{res['id']}")
    kb.button(text="❌ رفض", callback_data=f"pay:reject:{res['id']}")
    kb.adjust(2)
    q = await owner_quality(db, cb.from_user.id)
    ret = await retention_sample(bot, db, res["id"])
    flag = await db.val("SELECT reason FROM flags WHERE owner_id=?", cb.from_user.id)
    report = (f"\n📈 الجودة: معتمد {q['valid']} • انتظار {q['pending']} • ملغي {q['invalid']} • قليلو التفاعل {q['inactive']}")
    if ret:
        report += f"\n🔎 عيّنة البقاء بقنواتك الإجبارية: {ret[1]} من {ret[0]}"
    if flag:
        report += f"\n🚨 مُعلَّم: {flag}"
    for uid in set(OWNER_IDS):
        await safe_send(bot, uid, f"💰 طلب صرف #{res['id']}\nالمستخدم: {mention(owner)} ({owner['user_id']})\n"
                                  f"المبلغ: {res['amount']}$ عن {res['count']} عضو معتمد{report}", reply_markup=kb.as_markup())
    await audit(db, cb.from_user.id, "payout_request", res["id"])


# ───────────── تتبع حالة البوت والقنوات والتعليقات ─────────────
@router.my_chat_member()
async def on_my_chat_member(ev: ChatMemberUpdated):
    st = ev.new_chat_member.status
    if ev.chat.type == "private":
        if st in ("kicked", "left"):
            await ctx.db.run("UPDATE users SET blocked_bot=1 WHERE user_id=?", ev.chat.id)
        elif st == "member":
            await ctx.db.run("UPDATE users SET blocked_bot=0 WHERE user_id=?", ev.chat.id)
    elif ev.chat.type == "channel":
        if st in ("kicked", "left"):
            await ctx.db.run("UPDATE channels SET active=0 WHERE chat_id=?", ev.chat.id)
        elif st == "administrator":
            await ctx.db.run("UPDATE channels SET active=1 WHERE chat_id=?", ev.chat.id)


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def group_text(m: Message):
    if not m.from_user or m.from_user.is_bot:
        return
    rows = await ctx.db.all("SELECT id, comment_text FROM events WHERE status='active' AND comment_chat=? "
                            "AND comment_text IS NOT NULL", m.chat.id)
    for r in rows:
        if r["comment_text"].lower() in m.text.lower():
            await ctx.db.run("INSERT OR IGNORE INTO comment_hits(event_id,user_id) VALUES(?,?)", r["id"], m.from_user.id)


# ───────────── البوابة، ملفي، النشر، الفعاليات الجارية، سجل الفائزين ─────────────
@router.callback_query(F.data == "gate:check")
async def gate_check(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    await ctx.db.touch_user(cb.from_user)
    clear_gate(uid)
    lang = await lang_of(uid)
    if not ctx.is_admin(uid) and await gate_missing(bot, ctx.db, uid):
        return await cb.answer(t(lang, "gate_no"), show_alert=True)
    await cb.answer("✅")
    await gate(cb.message, uid, bot)


@router.callback_query(F.data.startswith("myrank:"))
async def myrank(cb: CallbackQuery):
    rows = await ctx.db.all(
        "SELECT c.user_id, COUNT(v.voter_id) AS votes FROM candidates c LEFT JOIN votes v ON v.candidate_id=c.id "
        "WHERE c.event_id=? GROUP BY c.id ORDER BY votes DESC, c.created_at", int(cb.data[7:]))
    for i, r in enumerate(rows):
        if r["user_id"] == cb.from_user.id:
            txt = f"📊 ترتيبك: {i + 1} من {len(rows)}\n🗳 أصواتك: {r['votes']}"
            if i > 0:
                txt += f"\n⬆️ تحتاج {rows[i - 1]['votes'] - r['votes'] + 1} صوت للمركز الأعلى"
            return await cb.answer(txt, show_alert=True)
    await cb.answer("لست مسجلاً كمتسابق", show_alert=True)


@router.callback_query(F.data == "m:me")
async def m_me(cb: CallbackQuery):
    db, uid = ctx.db, cb.from_user.id
    lang = await lang_of(uid)
    joined = await db.val("SELECT COUNT(*) FROM participants WHERE user_id=?", uid)
    voted = await db.val("SELECT COUNT(*) FROM votes WHERE voter_id=?", uid)
    wins = await db.val("SELECT COUNT(*) FROM winners WHERE user_id=?", uid)
    made = await db.val("SELECT COUNT(*) FROM events WHERE owner_id=?", uid)
    q = await owner_quality(db, uid)
    txt = (f"👤 <b>ملفي</b>\n\n🎡 شاركت بـ {joined} فعالية • 🏆 فزت {wins} مرة • 🗳 صوّتّ {voted} مرة\n"
           f"🎯 فعالياتي: {made}\n🔗 جلبت للبوت: {q['valid'] + q['pending']} (معتمد {q['valid']})")
    await show(cb, txt, home_kb(lang))
    await cb.answer()


@router.callback_query(F.data == "m:share")
async def m_share(cb: CallbackQuery):
    uid = cb.from_user.id
    lang = await lang_of(uid)
    code = await ensure_default_link(ctx.db, uid)
    link = f"https://t.me/{ctx.bot_username}?start=ref_{code}"
    promo = "🎡 بوت روليت ومسابقات: انشر فعاليتك بقناتك وكبّرها، وشارك واربح جوائز!"
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 شارك الآن", url="https://t.me/share/url?url=" + quote(link) + "&text=" + quote(promo))
    kb.button(text=t(lang, "btn_home"), callback_data="m:home")
    kb.adjust(1)
    await show(cb, f"📣 <b>انشر البوت</b>\nكل من يفعّل البوت عبر رابطك يُحتسب لك.\n\n{promo}\n{link}", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "dir:live")
async def dir_live(cb: CallbackQuery):
    rows = await ctx.db.all("SELECT id,kind,title,featured FROM events WHERE status='active' AND end_at>? "
                            "ORDER BY featured DESC, end_at LIMIT 15", now())
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"{'⭐ ' if r['featured'] else ''}{KIND_ICON.get(r['kind'], '')} {r['title']}", callback_data=f"ev:{r['id']}")
    kb.button(text="⬅️", callback_data="m:dir")
    kb.adjust(1)
    await show(cb, "🔴 <b>الفعاليات الجارية الآن</b>" + ("" if rows else "\n\nلا توجد فعاليات حالياً."), kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "dir:wins")
async def dir_wins(cb: CallbackQuery):
    rows = await ctx.db.all(
        "SELECT e.title, w.place, u.user_id, u.first_name, u.username FROM winners w JOIN events e ON e.id=w.event_id "
        "JOIN users u ON u.user_id=w.user_id ORDER BY e.end_at DESC, w.place LIMIT 15")
    txt = "🏅 <b>سجل الفائزين</b>\n\n" + ("\n".join(f"{esc(r['title'])} — #{r['place']} {mention(r)}" for r in rows) or "لا فائزين بعد.")
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️", callback_data="m:dir")
    await show(cb, txt, kb.as_markup())
    await cb.answer()
