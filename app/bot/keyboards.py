from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def kb_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👤 Личный кабинет", callback_data="nav:cabinet")
    b.button(text="🟡 Yandex Plus", callback_data="nav:yandex")
    b.button(text="🌍 VPN", callback_data="nav:vpn")
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
    b.button(text="💳 Продлить на 1 мес", callback_data="pay:mock:1m")
    if is_owner:
        b.button(text="🛠 Админка", callback_data="admin:menu")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_pay() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Тест-оплата 299 ₽ (успех)", callback_data="pay:mock:1m")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_vpn() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📖 Инструкция", callback_data="vpn:guide")
    b.button(text="📦 Отправить конфиг + QR", callback_data="vpn:bundle")
    b.button(text="♻️ Сбросить VPN", callback_data="vpn:reset:confirm")
    b.button(text="⬅️ Назад", callback_data="nav:vpn")
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
    b.button(text="➕ Добавить Yandex-аккаунт", callback_data="admin:yandex:add")
    b.button(text="📋 Список аккаунтов", callback_data="admin:yandex:list")
    b.button(text="🔍 Проверить Yandex аккаунт", callback_data="admin:yandex:probe")

    # ➕ НОВАЯ КНОПКА
    b.button(text="🧨 Сбросить пользователя (TEST)", callback_data="admin:reset:user")

    b.button(text="🏠 Главное меню", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()
