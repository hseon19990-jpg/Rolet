import os


def _ids(v):
    out = set()
    for x in (v or "").replace(" ", "").split(","):
        if x.lstrip("-").isdigit():
            out.add(int(x))
    return out


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# ايدي المالك/المالكين (مفصولة بفاصلة)
OWNER_IDS = _ids(os.getenv("ADMIN_IDS", ""))
# على Railway اربط Volume على المسار /data حتى ما تضيع البيانات
DB_PATH = os.getenv("DB_PATH") or ("/data/bot.db" if os.path.isdir("/data") else "bot.db")

# كل هذي الإعدادات تتغير من داخل البوت بالأمر /set
DEFAULTS = {
    "ref_threshold": "1500",   # عدد الأعضاء المعتمدين لكل مكافأة
    "ref_reward": "5",         # المكافأة بالدولار لكل دفعة
    "hold_days": "7",          # أيام انتظار قبل اعتماد العضو
    "captcha_on": "0",         # كابتشا عامة عند /start (الافتراضي مطفأة: التحقق صار يُطلب حسب كل فعالية يحددها منظّمها)
    "require_username": "0",   # اشتراط يوزر للمشاركة
    "require_photo": "0",      # اشتراط صورة للمشاركة
    "max_active_events": "5",  # الحد الأقصى للفعاليات النشطة لكل عضو
    "vote_spike": "30",        # أصوات بدقيقة وحدة تعتبر مشبوهة
    "backup_daily": "1",       # نسخة احتياطية يومية ترسل للمالك
    "feat_roulette": "1",
    "feat_votes": "1",
    "feat_directory": "1",
    "feat_chroulette": "1",     # رولت القنوات (للمشرفين)
    "chroulette_max_channels": "10",  # أقصى عدد قنوات لكل مشارك = أقصى عدد فرص
    "gate_on": "1",            # اشتراك إجباري عام: لا أحد يستخدم البوت قبل الاشتراك
    "maintenance": "0",        # وضع الصيانة: يوقف البوت عن غير المشرفين
    "vote_min_age": "10",      # دقائق لازم تمر على دخول الحساب للبوت قبل ما يصوّت
    "burst_limit": "40",       # عدد إحالات بـ10 دقائق لنفس المنظّم يعتبر رشق مشبوه
    "weekly_post": "1",        # نشر أفضل المنظّمين أسبوعياً بقناتك الإجبارية
    "last_backup": "0",
    "last_weekly": "0",
    "last_digest": "0",
}
EDITABLE = [k for k in DEFAULTS if not k.startswith("last_")]
