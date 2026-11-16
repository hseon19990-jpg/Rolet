import asyncio
import csv
import io
from html import escape as esc

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ctx
from config import EDITABLE
from services import audit, decide_payout, mention, now, owner_quality, safe_send, top_channels
from ui import channel_url, resolve_channel, resolve_channel_loose, show

router = Router()


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return bool(event.from_user) and ctx.is_admin(event.from_user.id)


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class BC(StatesGroup):
    wait = State()


class AddMand(StatesGroup):
    wait = State()


def owner_only(uid):
    return ctx.is_owner(uid)


# ───────────── اللوحة ─────────────
def admin_kb():
    kb = InlineKeyboardBuilder()
    for text, cb in (("📊 الإحصائيات", "adm:stats"), ("💰 طلبات الصرف", "adm:pay"), ("🏆 الأكثر جلباً", "adm:top"),
                     ("📢 القنوات الإجبارية", "adm:mand"), ("🚩 البلاغات", "adm:rep"), ("🎯 الفعاليات النشطة", "adm:ev"),
                     ("⚙️ الإعدادات", "adm:set"), ("📤 تصدير CSV", "adm:csv"), ("🗄 نسخة احتياطية", "adm:bk"),
                     ("📣 إذاعة", "adm:bc"), ("📖 الأوامر", "adm:help"), ("🏠", "m:home")):
        kb.button(text=text, callback_data=cb)
    kb.adjust(2)
    return kb.as_markup()


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    await m.answer("⚙️ <b>لوحة التحكم</b>", reply_markup=admin_kb())


@router.callback_query(F.data == "adm:home")
async def adm_home(cb: CallbackQuery):
    await show(cb, "⚙️ <b>لوحة التحكم</b>", admin_kb())
    await cb.answer()


def back_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ اللوحة", callback_data="adm:home")
    return kb.as_markup()


@router.callback_query(F.data == "adm:help")
async def adm_help(cb: CallbackQuery):
    await show(cb, (
        "📖 <b>الأوامر</b>\n"
        "/ban ID • /unban ID • /user ID\n"
        "/cancel_event ID • /feature ID (تثبيت/إلغاء تثبيت فعالية)\n/flags • /unflag ID (المنظّمون المشتبه بهم)\n"
        "/addmand @قناة • /delmand ID\n"
        "<i>للمالك فقط:</i>\n"
        "/set المفتاح القيمة\n"
        "/linkprice كود السعر • /linkthr كود العدد\n"
        "/addmod ID • /delmod ID\n"
        "/broadcast [ar|en|all]"), back_kb())
    await cb.answer()


# ───────────── الإحصائيات ─────────────
async def stats_text():
    db = ctx.db
    v = db.val
    day = now() - 86400
    users = await v("SELECT COUNT(*) FROM users")
    verified = await v("SELECT COUNT(*) FROM users WHERE verified=1")
    new24 = await v("SELECT COUNT(*) FROM users WHERE created_at>=?", day)
    blocked = await v("SELECT COUNT(*) FROM users WHERE blocked_bot=1")
    ref = {r["status"]: r["n"] for r in await db.all("SELECT status, COUNT(*) AS n FROM referrals GROUP BY status")}
    ev_active = await v("SELECT COUNT(*) FROM events WHERE status='active'")
    ev_total = await v("SELECT COUNT(*) FROM events")
    chans = await v("SELECT COUNT(*) FROM channels WHERE active=1")
    pend = await v("SELECT COUNT(*) FROM payouts WHERE status='pending'")
    owed = await v("SELECT COALESCE(SUM(amount),0) FROM payouts WHERE status IN ('pending','approved')")
    paid = await v("SELECT COALESCE(SUM(amount),0) FROM payouts WHERE status='paid'")
    tops = await top_channels(db, 0, 5)
    orgs = await db.all("SELECT u.first_name,u.username,u.user_id, COUNT(*) AS n FROM referrals r "
                        "JOIN users u ON u.user_id=r.owner_id WHERE r.status IN ('pending','valid') "
                        "GROUP BY r.owner_id ORDER BY n DESC LIMIT 5")
    txt = (f"📊 <b>الإحصائيات</b>\n\n👥 المستخدمون: {users} (متحققون {verified}) • جدد 24س: {new24} • حظروا البوت: {blocked}\n"
           f"🔗 الإحالات: معتمد {ref.get('valid', 0)} • انتظار {ref.get('pending', 0)} • ملغي {ref.get('invalid', 0)}\n"
           f"🎯 الفعاليات: نشطة {ev_active} من {ev_total} • قنوات: {chans}\n"
           f"💰 طلبات صرف معلقة: {pend} • مستحق: {owed}$ • مدفوع: {paid}$\n")
    if tops:
        txt += "\n🔥 أعلى القنوات:\n" + "\n".join(f"{i}. {esc(r['title'] or '')} — {r['brought']} (معتمد {r['valid']})"
                                                    for i, r in enumerate(tops, 1))
    if orgs:
        txt += "\n\n🥇 أعلى المنظّمين:\n" + "\n".join(
            f"{i}. {esc(r['first_name'] or r['username'] or str(r['user_id']))} — {r['n']}" for i, r in enumerate(orgs, 1))
    return txt


@router.callback_query(F.data == "adm:stats")
async def adm_stats(cb: CallbackQuery):
    await show(cb, await stats_text(), back_kb())
    await cb.answer()


@router.callback_query(F.data == "adm:top")
async def adm_top(cb: CallbackQuery):
    rows = await top_channels(ctx.db, 0, 20)
    txt = "🏆 <b>الأكثر جلباً</b>\n\n" + ("\n".join(
        f"{i}. {esc(r['title'] or '')} — جلب {r['brought']} • معتمد {r['valid']}" for i, r in enumerate(rows, 1)) or "—")
    await show(cb, txt, back_kb())
    await cb.answer()


# ───────────── طلبات الصرف ─────────────
@router.callback_query(F.data == "adm:pay")
async def adm_pay(cb: CallbackQuery):
    if not owner_only(cb.from_user.id):
        return await cb.answer("للمالك فقط", show_alert=True)
    rows = await ctx.db.all("SELECT * FROM payouts WHERE status IN ('pending','approved') ORDER BY id LIMIT 15")
    kb = InlineKeyboardBuilder()
    lines = ["💰 <b>طلبات الصرف</b>\n"]
    for p in rows:
        u = await ctx.db.one("SELECT user_id,username,first_name FROM users WHERE user_id=?", p["owner_id"]) or \
            {"user_id": p["owner_id"], "first_name": str(p["owner_id"]), "username": None}
        lines.append(f"#{p['id']} • {mention(u)} • {p['amount']}$ • {p['count']} عضو • {p['status']}")
        if p["status"] == "pending":
            kb.button(text=f"✅ #{p['id']}", callback_data=f"pay:approve:{p['id']}")
            kb.button(text=f"❌ #{p['id']}", callback_data=f"pay:reject:{p['id']}")
        else:
            kb.button(text=f"💵 تم الدفع #{p['id']}", callback_data=f"pay:paid:{p['id']}")
    kb.button(text="⬅️ اللوحة", callback_data="adm:home")
    kb.adjust(2, 1)
    await show(cb, "\n".join(lines) if rows else "💰 لا توجد طلبات.", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.regexp(r"^pay:(approve|reject|paid):\d+$"))
async def pay_decide(cb: CallbackQuery, bot: Bot):
    if not owner_only(cb.from_user.id):
        return await cb.answer("للمالك فقط", show_alert=True)
    _, action, pid = cb.data.split(":")
    res = await decide_payout(ctx.db, int(pid), action, cb.from_user.id)
    if res is None or res == "bad_state":
        return await cb.answer("الطلب غير موجود أو تمت معالجته.", show_alert=True)
    msgs = {"approve": "✅ تمت الموافقة على طلب الصرف، سيصلك الدفع قريباً.",
            "reject": "❌ تم رفض طلب الصرف. تم إرجاع الأعضاء لرصيدك، راجع القواعد أو تواصل مع الإدارة.",
            "paid": "💵 تم دفع مكافأتك. شكراً لك!"}
    await safe_send(bot, res["owner_id"], f"{msgs[action]}\nطلب #{res['id']} • {res['amount']}$")
    await cb.answer("تم")
    try:
        await cb.message.edit_text((cb.message.html_text or "") + f"\n\n➡️ {action} بواسطة {cb.from_user.id}")
    except Exception:
        pass


# ───────────── القنوات الإجبارية ─────────────
@router.callback_query(F.data == "adm:mand")
async def adm_mand(cb: CallbackQuery):
    rows = await ctx.db.all("SELECT * FROM mandatory_channels")
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"🗑 {r['title']}", callback_data=f"mand:del:{r['chat_id']}")
    if owner_only(cb.from_user.id):
        kb.button(text="➕ إضافة قناة إجبارية", callback_data="mand:add")
    kb.button(text="⬅️ اللوحة", callback_data="adm:home")
    kb.adjust(1)
    await show(cb, "📢 <b>القنوات الإجبارية العامة</b> (تُطبَّق على كل الفعاليات)\n"
                   "اضغط «إضافة» وأرسل القناة بأي شكل (رابط / @يوزر / ايدي / منشور محوّل).\n"
                   + ("" if rows else "\nلا توجد قنوات."), kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "mand:add")
async def mand_add(cb: CallbackQuery, state: FSMContext):
    if not owner_only(cb.from_user.id):
        return await cb.answer("للمالك فقط", show_alert=True)
    await state.clear()
    await state.set_state(AddMand.wait)
    await cb.answer()
    await cb.message.answer("📢 أرسل القناة الإجبارية: رابط أو @يوزر أو ايدي أو حوّل منشوراً منها.\n"
                            "(للإلغاء: /cancel)")


async def _save_mand(m: Message, state: FSMContext, bot: Bot):
    if not owner_only(m.from_user.id):
        return
    chat, note, err = await resolve_channel_loose(m, bot)
    if err:
        return await m.answer(err)
    url = await channel_url(bot, chat)
    await ctx.db.run("INSERT INTO mandatory_channels(chat_id,title,username,url,added_at) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username, url=excluded.url",
                     chat.id, chat.title, chat.username, url, now())
    await audit(ctx.db, m.from_user.id, "mand_add", chat.id)
    await state.clear()
    await m.answer(f"✅ أضفت «{esc(chat.title or '')}» كقناة إجبارية عامة." + (f"\n\n{note}" if note else ""),
                   reply_markup=back_kb())


@router.message(StateFilter(AddMand.wait), F.forward_origin)
async def mand_in_fwd(m: Message, state: FSMContext, bot: Bot):
    await _save_mand(m, state, bot)


@router.message(StateFilter(AddMand.wait), F.text, ~F.text.startswith("/"))
async def mand_in_text(m: Message, state: FSMContext, bot: Bot):
    await _save_mand(m, state, bot)


@router.callback_query(F.data.startswith("mand:del:"))
async def mand_del(cb: CallbackQuery):
    if not owner_only(cb.from_user.id):
        return await cb.answer("للمالك فقط", show_alert=True)
    await ctx.db.run("DELETE FROM mandatory_channels WHERE chat_id=?", int(cb.data.split(":")[2]))
    await audit(ctx.db, cb.from_user.id, "mand_del", cb.data)
    await cb.answer("🗑 تم")
    await adm_mand(cb)


@router.message(Command("addmand"))
async def cmd_addmand(m: Message, bot: Bot):
    if not owner_only(m.from_user.id):
        return await m.answer("للمالك فقط")
    chat, err = await resolve_channel(m.model_copy(update={"text": (m.text or "").split(maxsplit=1)[-1]}), bot,
                                      need_user_admin=False)
    if err:
        return await m.answer(err)
    url = await channel_url(bot, chat)
    await ctx.db.run("INSERT INTO mandatory_channels(chat_id,title,username,url,added_at) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username, url=excluded.url",
                     chat.id, chat.title, chat.username, url, now())
    await audit(ctx.db, m.from_user.id, "mand_add", chat.id)
    await m.answer(f"✅ أضفت «{esc(chat.title or '')}» كقناة إجبارية عامة.")


@router.message(Command("delmand"))
async def cmd_delmand(m: Message, command: CommandObject):
    if not owner_only(m.from_user.id) or not (command.args or "").lstrip("-").isdigit():
        return await m.answer("الاستخدام: /delmand ID")
    await ctx.db.run("DELETE FROM mandatory_channels WHERE chat_id=?", int(command.args))
    await m.answer("🗑 تم")


# ───────────── المستخدمون والحظر ─────────────
@router.message(Command("ban"))
async def cmd_ban(m: Message, command: CommandObject):
    a = (command.args or "").split()
    if not a or not a[0].isdigit():
        return await m.answer("الاستخدام: /ban ID")
    uid = int(a[0])
    if ctx.is_admin(uid):
        return await m.answer("ما تكدر تحظر مشرف.")
    await ctx.db.run("UPDATE users SET banned=1 WHERE user_id=?", uid)
    await ctx.db.run("UPDATE referrals SET status='invalid' WHERE user_id=? AND claim_id IS NULL", uid)
    await audit(ctx.db, m.from_user.id, "ban", uid)
    await m.answer(f"🚫 تم حظر {uid}")


@router.message(Command("unban"))
async def cmd_unban(m: Message, command: CommandObject):
    if not (command.args or "").isdigit():
        return await m.answer("الاستخدام: /unban ID")
    await ctx.db.run("UPDATE users SET banned=0 WHERE user_id=?", int(command.args))
    await audit(ctx.db, m.from_user.id, "unban", command.args)
    await m.answer("✅ تم")


@router.message(Command("user"))
async def cmd_user(m: Message, command: CommandObject):
    if not (command.args or "").isdigit():
        return await m.answer("الاستخدام: /user ID")
    uid = int(command.args)
    u = await ctx.db.one("SELECT * FROM users WHERE user_id=?", uid)
    if not u:
        return await m.answer("غير موجود")
    ref = await ctx.db.one("SELECT * FROM referrals WHERE user_id=?", uid)
    q = await owner_quality(ctx.db, uid)
    flag = await ctx.db.val("SELECT reason FROM flags WHERE owner_id=?", uid)
    brought = await ctx.db.val("SELECT COUNT(*) FROM referrals WHERE owner_id=? AND status IN ('pending','valid')", uid)
    await m.answer(f"👤 {mention(u)} ({uid})\nيوزر: @{u['username']}\nمتحقق: {u['verified']} • محظور: {u['banned']} • "
                   f"حظر البوت: {u['blocked_bot']} • بريميوم: {u['is_premium']}\n"
                   f"جاء عبر: {ref['owner_id'] if ref else '—'} ({ref['status'] if ref else '—'})\nجلب: {brought}\n📈 جودة: معتمد {q['valid']} • انتظار {q['pending']} • ملغي {q['invalid']} • قليلو التفاعل {q['inactive']}"
                   + (f"\n🚨 مُعلَّم: {flag}" if flag else ""))


@router.message(Command("addmod"))
async def cmd_addmod(m: Message, command: CommandObject):
    if not owner_only(m.from_user.id) or not (command.args or "").isdigit():
        return await m.answer("للمالك: /addmod ID")
    await ctx.db.run("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", int(command.args))
    ctx.mods.add(int(command.args))
    await audit(ctx.db, m.from_user.id, "addmod", command.args)
    await m.answer("✅ صار مشرفاً (يدير الفعاليات والبلاغات والحظر، بدون الدفع والإعدادات).")


@router.message(Command("delmod"))
async def cmd_delmod(m: Message, command: CommandObject):
    if not owner_only(m.from_user.id) or not (command.args or "").isdigit():
        return await m.answer("للمالك: /delmod ID")
    await ctx.db.run("DELETE FROM admins WHERE user_id=?", int(command.args))
    ctx.mods.discard(int(command.args))
    await m.answer("✅ تم")


# ───────────── الفعاليات والبلاغات ─────────────
@router.callback_query(F.data == "adm:ev")
async def adm_ev(cb: CallbackQuery):
    rows = await ctx.db.all("SELECT id,kind,title,owner_id FROM events WHERE status='active' ORDER BY id DESC LIMIT 20")
    txt = "🎯 <b>الفعاليات النشطة</b>\n(للإلغاء: /cancel_event ID)\n\n" + (
        "\n".join(f"#{r['id']} {'🎡' if r['kind'] == 'roulette' else '🏆'} {esc(r['title'])} • {r['owner_id']}" for r in rows) or "—")
    await show(cb, txt, back_kb())
    await cb.answer()


@router.message(Command("cancel_event"))
async def cmd_cancel_event(m: Message, command: CommandObject):
    if not (command.args or "").isdigit():
        return await m.answer("الاستخدام: /cancel_event ID")
    n = await ctx.db.run("UPDATE events SET status='cancelled' WHERE id=? AND status='active'", int(command.args))
    await audit(ctx.db, m.from_user.id, "cancel_event", command.args)
    await m.answer("⛔ تم الإلغاء" if n else "غير موجودة أو غير نشطة")


@router.callback_query(F.data == "adm:rep")
async def adm_rep(cb: CallbackQuery):
    rows = await ctx.db.all("SELECT event_id, COUNT(*) AS n FROM reports GROUP BY event_id ORDER BY n DESC LIMIT 15")
    txt = "🚩 <b>البلاغات</b>\n\n" + ("\n".join(f"فعالية #{r['event_id']} — {r['n']} بلاغ" for r in rows) or "لا توجد بلاغات.")
    await show(cb, txt, back_kb())
    await cb.answer()


# ───────────── الإعدادات ─────────────
@router.callback_query(F.data == "adm:set")
async def adm_set(cb: CallbackQuery):
    lines = ["⚙️ <b>الإعدادات</b> (للتعديل: /set المفتاح القيمة)\n"]
    lines += [f"<code>{k}</code> = {ctx.db.get(k)}" for k in EDITABLE]
    await show(cb, "\n".join(lines), back_kb())
    await cb.answer()


@router.message(Command("set"))
async def cmd_set(m: Message, command: CommandObject):
    if not owner_only(m.from_user.id):
        return await m.answer("للمالك فقط")
    a = (command.args or "").split()
    if len(a) != 2 or a[0] not in EDITABLE:
        return await m.answer("الاستخدام: /set المفتاح القيمة\nالمفاتيح: " + ", ".join(EDITABLE))
    try:
        float(a[1])
    except ValueError:
        return await m.answer("القيمة لازم تكون رقم (0 أو 1 للتشغيل/الإيقاف).")
    await ctx.db.set(a[0], a[1])
    await audit(ctx.db, m.from_user.id, "set", f"{a[0]}={a[1]}")
    await m.answer(f"✅ {a[0]} = {a[1]}")


@router.message(Command("linkprice"))
async def cmd_linkprice(m: Message, command: CommandObject):
    a = (command.args or "").split()
    if not owner_only(m.from_user.id) or len(a) != 2:
        return await m.answer("للمالك: /linkprice كود السعر  (مثال: /linkprice ab12cd34 3.5 — أو - لإرجاع الافتراضي)")
    try:
        price = None if a[1] == "-" else float(a[1])
    except ValueError:
        return await m.answer("السعر لازم رقم.")
    n = await ctx.db.run("UPDATE invite_links SET price=? WHERE code=?", price, a[0])
    await audit(ctx.db, m.from_user.id, "linkprice", f"{a[0]}={a[1]}")
    await m.answer("✅ تم" if n else "الكود غير موجود")


@router.message(Command("linkthr"))
async def cmd_linkthr(m: Message, command: CommandObject):
    a = (command.args or "").split()
    if not owner_only(m.from_user.id) or len(a) != 2 or not (a[1].isdigit() or a[1] == "-"):
        return await m.answer("للمالك: /linkthr كود العدد  (أو - لإرجاع الافتراضي)")
    n = await ctx.db.run("UPDATE invite_links SET threshold=? WHERE code=?", None if a[1] == "-" else int(a[1]), a[0])
    await audit(ctx.db, m.from_user.id, "linkthr", f"{a[0]}={a[1]}")
    await m.answer("✅ تم" if n else "الكود غير موجود")


# ───────────── التصدير والنسخ الاحتياطي ─────────────
def to_csv(rows):
    out = io.StringIO()
    if rows:
        w = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


@router.callback_query(F.data == "adm:csv")
async def adm_csv(cb: CallbackQuery):
    if not owner_only(cb.from_user.id):
        return await cb.answer("للمالك فقط", show_alert=True)
    await cb.answer("جاري التصدير...")
    for name, sql in (("referrals", "SELECT * FROM referrals"), ("payouts", "SELECT * FROM payouts"),
                      ("events", "SELECT * FROM events"), ("audit", "SELECT * FROM audit ORDER BY id DESC LIMIT 5000")):
        rows = await ctx.db.all(sql)
        if rows:
            await cb.message.answer_document(BufferedInputFile(to_csv(rows), filename=f"{name}.csv"))


@router.callback_query(F.data == "adm:bk")
@router.message(Command("backup"))
async def backup(ev):
    if not owner_only(ev.from_user.id):
        return
    path = f"/tmp/backup_{now()}.db"
    await asyncio.to_thread(ctx.db.backup_to, path)
    target = ev.message if isinstance(ev, CallbackQuery) else ev
    await target.answer_document(FSInputFile(path), caption="🗄 نسخة احتياطية")
    if isinstance(ev, CallbackQuery):
        await ev.answer()


# ───────────── الإذاعة ─────────────
@router.message(Command("broadcast"))
async def cmd_broadcast(m: Message, command: CommandObject, state: FSMContext):
    if not owner_only(m.from_user.id):
        return await m.answer("للمالك فقط")
    scope = (command.args or "all").strip()
    if scope not in ("all", "ar", "en"):
        scope = "all"
    await state.set_state(BC.wait)
    await state.update_data(scope=scope)
    await m.answer(f"📣 أرسل الرسالة اللي تريد إذاعتها (الفئة: {scope}). للإلغاء /cancel")


@router.callback_query(F.data == "adm:bc")
async def adm_bc(cb: CallbackQuery):
    await cb.message.answer("📣 للإذاعة استخدم: /broadcast [all|ar|en]")
    await cb.answer()


@router.message(Command("cancel"))
async def adm_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("تم الإلغاء.")


@router.message(BC.wait)
async def bc_msg(m: Message, state: FSMContext):
    d = await state.get_data()
    where, args = "banned=0 AND blocked_bot=0", []
    if d["scope"] != "all":
        where += " AND lang=?"
        args.append(d["scope"])
    n = await ctx.db.val(f"SELECT COUNT(*) FROM users WHERE {where}", *args)
    await state.update_data(chat_id=m.chat.id, mid=m.message_id, where=where, args=args)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ إرسال إلى {n}", callback_data="bc:go")
    kb.button(text="❌ إلغاء", callback_data="bc:no")
    kb.adjust(1)
    await m.answer("تأكيد الإذاعة؟", reply_markup=kb.as_markup())


@router.callback_query(F.data == "bc:no")
async def bc_no(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("أُلغيت")


@router.callback_query(F.data == "bc:go")
async def bc_go(cb: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    if not d.get("mid"):
        return await cb.answer("انتهت الجلسة")
    await cb.answer("بدأت الإذاعة")
    asyncio.create_task(run_broadcast(bot, cb.from_user.id, d))


async def run_broadcast(bot, admin_id, d):
    rows = await ctx.db.all(f"SELECT user_id FROM users WHERE {d['where']}", *d["args"])
    ok = fail = 0
    for r in rows:
        while True:
            try:
                await bot.copy_message(r["user_id"], d["chat_id"], d["mid"])
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                await ctx.db.run("UPDATE users SET blocked_bot=1 WHERE user_id=?", r["user_id"])
                fail += 1
                break
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.05)
    await audit(ctx.db, admin_id, "broadcast", f"ok={ok} fail={fail}")
    await safe_send(bot, admin_id, f"📣 انتهت الإذاعة: وصلت {ok} • فشلت {fail}")


@router.message(Command("flags"))
async def cmd_flags(m: Message):
    rows = await ctx.db.all("SELECT * FROM flags ORDER BY created_at DESC LIMIT 20")
    await m.answer("🚨 <b>منظّمون مشتبه بهم</b>\n\n" + ("\n".join(f"<code>{r['owner_id']}</code> — {esc(r['reason'])}" for r in rows) or "لا أحد."))


@router.message(Command("unflag"))
async def cmd_unflag(m: Message, command: CommandObject):
    if not (command.args or "").isdigit():
        return await m.answer("الاستخدام: /unflag ID")
    await ctx.db.run("DELETE FROM flags WHERE owner_id=?", int(command.args))
    await audit(ctx.db, m.from_user.id, "unflag", command.args)
    await m.answer("✅ أُزيلت العلامة")


@router.message(Command("feature"))
async def cmd_feature(m: Message, command: CommandObject):
    if not (command.args or "").isdigit():
        return await m.answer("الاستخدام: /feature ID")
    n = await ctx.db.run("UPDATE events SET featured=1-featured WHERE id=?", int(command.args))
    await m.answer("⭐ تم التبديل" if n else "غير موجودة")
