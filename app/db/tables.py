import random
import string
from datetime import datetime
from enum import Enum
from typing import List

from piccolo.columns import BigInt, ForeignKey, Text, Timestamp, Varchar
from piccolo.columns.readable import Readable
from piccolo.table import Table

from app.config_reader import DB


def generate_promo_code(length=6):
    characters = string.ascii_uppercase + string.digits
    return "".join(random.choices(characters, k=length))


class FreelancePlatform(str, Enum):
    KWORK = "kwork"
    UPWORK = "upwork"


class Project(Table, db=DB):
    url = Varchar(length=1024, required=True)
    title = Varchar(required=True)
    description = Text(required=True)
    freelance_platform = Varchar(choices=FreelancePlatform, required=True)
    first_seen_at = Timestamp(default=datetime.now)
    # Волна 5: данные для кнопок (генерация отклика, перепроверка) и динамики
    kwork_price = BigInt(default=0)          # нижняя граница бюджета (реальный)
    offers_at_first = BigInt(default=0)      # N0 — число откликов при находке
    offers_rechecked = BigInt(default=-1)    # последнее N при перепроверке (-1 = не было)
    recheck_done = BigInt(default=0)         # счётчик авто-замеров динамики (0-4)
    next_recheck_at = BigInt(default=0)      # unix ts следующего авто-замера (0 = не запланирован)
    tg_message_id = BigInt(default=0)        # id отправленной карточки в TG (для edit динамики)
    card_text = Text(default="")             # исходный текст карточки (чтобы дописывать динамику через edit)
    tg_message_id = BigInt(default=0)        # id карточки в Telegram — авто-замер цепляем reply'ем


class PremiumUser(Table, db=DB):
    tg_id = BigInt(default=0)
    name = Varchar()
    invite_link = Varchar(default=generate_promo_code)

    @classmethod
    def get_readable(cls):
        return Readable("%s [%s]", [cls.name, cls.tg_id])


class Offer(Table, db=DB):
    project = ForeignKey(Project)
    offer = Text()
    offer_by = ForeignKey(PremiumUser)


PROJECT_TABLES: List[Table] = [Project, Offer, PremiumUser]
