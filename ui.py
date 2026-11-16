from aiogram.utils.keyboard import InlineKeyboardBuilder

from i18n import t


def menu_kb(lang, admin=False):
    kb = InlineKeyboardBuilder()
    for key, cb in (("btn_new", "m:new"), ("btn_events", "m:events"), ("btn_channels", "m:channels"),
                    ("btn_links", "m:links"), ("btn_dir", "m:dir"), ("btn_me", "m:me"), ("btn_share", "m:share"),
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
