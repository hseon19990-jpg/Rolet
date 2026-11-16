from config import OWNER_IDS

db = None
bot_username = ""
mods = set()


def is_owner(uid):
    return uid in OWNER_IDS


def is_admin(uid):
    return uid in OWNER_IDS or uid in mods
