from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def kb_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👤 Личный кабинет", callback_data="nav:cabinet")
    b.button(text="🟡 Yandex Plus", callback_data="nav:yandex")
    b.button(text="🌍 VPN", callback_data="nav:vpn")
    b.button(text="🎬 Кинотека", callback_data="nav:kinoteka")
    b.button(text="💳 Оплата", callback_data="nav:pay")
    b.button(text="❓ FAQ", callback_data="nav:faq")
    b.button(text="🛠 Поддержка", callback_data="nav:support")
    b.adjust(1)
    return b.as_markup()
def kb_back_home() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_cabinet(*, is_owner: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Продлить", callback_data="pay:buy:1m")
    b.button(text="👥 Рефералы", callback_data="nav:referrals")
    if is_owner:
        b.button(text="🛠 Админка", callback_data="admin:menu")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_pay() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Оплатить 299 ₽", callback_data="pay:buy:1m")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_vpn() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📖 Инструкция", callback_data="vpn:guide")
    b.button(text="📦 Отправить конфиг + QR", callback_data="vpn:bundle")
    b.button(text="🌍 Сменить локацию", callback_data="vpn:loc")
    b.button(text="♻️ Сбросить VPN", callback_data="vpn:reset:confirm")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_kinoteka() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔍 Поиск", callback_data="kino:search")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_kinoteka_back() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="nav:kinoteka")
    b.adjust(1)
    return b.as_markup()


def kb_vpn_guide_platforms() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📱 Android", callback_data="vpn:howto:android")
    b.button(text="🍎 iPhone / iPad", callback_data="vpn:howto:ios")
    b.button(text="💻 Windows", callback_data="vpn:howto:windows")
    b.button(text="🍏 macOS", callback_data="vpn:howto:macos")
    b.button(text="🐧 Linux", callback_data="vpn:howto:linux")
    b.button(text="⬅️ Назад", callback_data="nav:vpn")
    b.adjust(1)
    return b.as_markup()


def kb_vpn_guide_back() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="vpn:guide")
    b.adjust(1)
    return b.as_markup()


def kb_confirm_reset() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, сбросить", callback_data="vpn:reset")
    b.button(text="⬅️ Назад", callback_data="nav:vpn")
    b.adjust(1)
    return b.as_markup()


def kb_admin_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()

    # VPN
    b.button(text="📊 Статус VPN", callback_data="admin:vpn:status")
    b.button(text="👥 Активные VPN-профили", callback_data="admin:vpn:active_profiles")

    # Yandex
    b.button(text="➕ Добавить Yandex-аккаунт", callback_data="admin:yandex:add")
    b.button(text="📋 Список аккаунтов/слотов", callback_data="admin:yandex:list")
    b.button(text="✏️ Редактировать аккаунт", callback_data="admin:yandex:edit")

    # Kick reports
    b.button(text="📋 Кого исключить сегодня", callback_data="admin:kick:report")
    b.button(text="🧾 Отметить пользователя исключённым", callback_data="admin:kick:mark")

    # Finance / referrals
    b.button(text="💸 Заявки на вывод", callback_data="admin:payouts")
    b.button(text="⏳ Холды (рефералка)", callback_data="admin:ref:holds")
    b.button(text="🔁 Управление рефералами", callback_data="admin:referrals:menu")
    b.button(text="💲 Цена подписки", callback_data="admin:price")
    b.button(text="💰 Накрутить реф-баланс (TEST)", callback_data="admin:ref:mint")

    # Legacy / test
    b.button(text="🧽 Снять страйки Yandex", callback_data="admin:forgive:user")
    b.button(text="🧨 Сбросить пользователя (TEST)", callback_data="admin:reset:user")

    b.button(text="🏠 Главное меню", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_admin_referrals_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    # Важно: эти callback_data должны совпадать с хендлерами в admin.py
    # (иначе будет "Update ... is not handled").
    b.button(text="👑 Забрать реферала себе", callback_data="admin:ref:take:self")
    b.button(text="🔁 Назначить реферала", callback_data="admin:ref:assign")
    b.button(text="🔍 Узнать владельца", callback_data="admin:ref:owner")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    b.adjust(1)
    return b.as_markup()


def kb_back_faq() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="nav:faq")
    b.adjust(1)
    return b.as_markup()


def kb_faq() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="ℹ️ О сервисе", callback_data="faq:about")
    b.button(text="📄 Публичная оферта", callback_data="faq:offer")
    b.button(text="🔐 Политика конфиденциальности", callback_data="faq:privacy")
    b.button(text="📝 Пользовательское соглашение", callback_data="faq:terms")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()
