from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

import ctx
from i18n import t
from services import gate_missing, now
from ui import gate_screen


class GateMiddleware(BaseMiddleware):
    """الاشتراك الإجباري العام + وضع الصيانة + تتبع آخر نشاط. يطبق على كل مستخدم غير مشرف."""

    async def __call__(self, handler, event, data):
        user = event.from_user
        if isinstance(event, Message):
            chat = event.chat
        else:
            chat = event.message.chat if getattr(event, "message", None) else None
        # خارج المحادثة الخاصة (قنوات/مجموعات) لا نطبق البوابة
        if user is None or chat is None or chat.type != "private" or ctx.is_admin(user.id):
            return await handler(event, data)
        db = ctx.db
        await db.run("UPDATE users SET last_seen=? WHERE user_id=?", now(), user.id)
        lang = await db.val("SELECT lang FROM users WHERE user_id=?", user.id) or "ar"
        target = event if isinstance(event, Message) else event.message
        if db.get("maintenance") == "1":
            if isinstance(event, CallbackQuery):
                await event.answer(t(lang, "maint"), show_alert=True)
            else:
                await event.answer(t(lang, "maint"))
            return None
        # /start و«تحقق» يمرّان: معالجهما يطبق البوابة بنفسه بعد تسجيل الإحالة
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "gate:check":
            return await handler(event, data)
        missing = await gate_missing(data["bot"], db, user.id)
        if missing:
            if isinstance(event, CallbackQuery):
                await event.answer()
            await gate_screen(target, lang, missing)
            return None
        return await handler(event, data)
