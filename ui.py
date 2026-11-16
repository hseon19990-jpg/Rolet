from aiogram.utils.keyboard import InlineKeyboardBuilder

from i18n import t


def menu_kb(lang, admin=False):
    kb = InlineKeyboardBuilder()
    for key, cb in (("btn_new", "m:new"), ("btn_events", "m:events"), ("btn_channels", "m:channels"),
                    ("btn_links", "m:links"), ("btn_wd", "m:wd"), ("btn_dir", "m:dir"), ("btn_me", "m:me"), ("btn_share", "m:share"),
                    ("btn_rules", "m:rules"), ("btn_lang", "m:lang")):
        kb.button(text=t(lang, key), callback_data=cb)
    if admin:
        kb.button(text=t(lang, "btn_admin"), callback_data="adm:home")
    kb.adjust(2)
    return kb.as_markup()


def home_kb(lang):
    kb = InlineKeyboardBuilder()
    kb.button(text=t(lang, "btn_home"), callback_data="m:home")
    return kb.as_markup()


async def gate_screen(target, lang, missing):
    kb = InlineKeyboardBuilder()
    for ch in missing:
        if ch.get("url"):
            kb.button(text=f"📢 {ch.get('title') or ''}", url=ch["url"])
    kb.button(text=t(lang, "btn_retry"), callback_data="gate:check")
    kb.adjust(1)
    await target.answer(t(lang, "gate"), reply_markup=kb.as_markup())


async def show(cb, text, kb=None):
    """يعدّل الرسالة الحالية، وإذا تعذر يرسل رسالة جديدة."""
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


async def resolve_channel(m, bot, need_user_admin=True):
    """يستخرج القناة من منشور محوّل أو @يوزر أو ايدي ويتحقق من الصلاحيات.
    يرجع (chat, None) أو (None, رسالة_خطأ)"""
    chat = None
    fo = getattr(m, "forward_origin", None)
    if fo is not None and getattr(fo, "chat", None) is not None:
        chat = fo.chat
    else:
        txt = (m.text or "").strip()
        if txt.startswith("https://t.me/"):
            txt = "@" + txt.rstrip("/").rsplit("/", 1)[-1]
        if txt.startswith("@") or txt.lstrip("-").isdigit():
            try:
                chat = await bot.get_chat(int(txt) if txt.lstrip("-").isdigit() else txt)
            except Exception:
                return None, "❌ لم أجد القناة. تأكد من اليوزر وأن البوت مشرف فيها."
    if chat is None or chat.type != "channel":
        return None, "أرسل @يوزر القناة، أو حوّل لي منشوراً منها."
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
        if me.status != "administrator":
            return None, "❌ اجعل البوت مشرفاً في القناة أولاً."
        if need_user_admin:
            um = await bot.get_chat_member(chat.id, m.from_user.id)
            if um.status not in ("administrator", "creator"):
                return None, "❌ لازم تكون مشرفاً في هذي القناة."
    except Exception:
        return None, "❌ ما قدرت أتحقق من الصلاحيات. تأكد أن البوت مشرف."
    return chat, None


async def channel_url(bot, chat):
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}"
    try:
        return await bot.export_chat_invite_link(chat.id)
    except Exception:
        return None


async def resolve_channel_loose(m, bot):
    """يقبل القناة بأي شكل (رابط / منشور / @يوزر / ايدي / منشور محوّل) حتى لو البوت غير مشرف.
    يرجع (chat, ملاحظة_أو_None, خطأ_أو_None)."""
    import re
    chat = None
    fo = getattr(m, "forward_origin", None)
    if fo is not None and getattr(fo, "chat", None) is not None:
        chat = fo.chat
    else:
        txt = (getattr(m, "text", None) or getattr(m, "caption", None) or "").strip()
        ref = None
        mm = re.search(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/([^\s/?]+)", txt, re.I)
        if mm:
            name = mm.group(1)
            if name == "c":
                mc = re.search(r"/c/(\d+)", txt)
                ref = int("-100" + mc.group(1)) if mc else None
            elif name.startswith("+") or name == "joinchat":
                return None, None, "❌ روابط الدعوة الخاصة لا أستطيع قراءتها. أرسل @يوزر القناة أو ايديها أو حوّل منشوراً منها."
            else:
                ref = "@" + name
        else:
            u = re.search(r"@([A-Za-z][\w]{3,})", txt)
            i = re.search(r"(?<![\w/])-?\d{6,}(?!\w)", txt)
            ref = ("@" + u.group(1)) if u else (int(i.group(0)) if i else None)
        if ref is None:
            return None, None, "أرسل رابط القناة أو @يوزرها أو ايديها أو حوّل منشوراً منها."
        try:
            chat = await bot.get_chat(ref)
        except Exception:
            return None, None, "❌ لم أجد القناة. تأكد من الرابط/اليوزر."
    if chat.type not in ("channel", "supergroup", "group"):
        return None, None, "❌ أرسل قناة أو مجموعة."
    try:
        admin = (await bot.get_chat_member(chat.id, bot.id)).status == "administrator"
    except Exception:
        admin = False
    note = None if admin else ("⚠️ البوت ليس مشرفاً هناك، فلن يستطيع فحص الاشتراك تلقائياً (يُسمح للجميع بالمرور). "
                               "اجعله مشرفاً ليعمل الفحص.")
    return chat, note, None
