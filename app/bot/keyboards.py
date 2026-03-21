from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def kb_main(*, show_trial: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👤 Личный кабинет", callback_data="nav:cabinet")
    b.button(text="🟡 Yandex Plus", callback_data="nav:yandex")
    b.button(text="🌍 VPN", callback_data="nav:vpn")
    b.button(text="📶 VPN LTE", callback_data="vpn:lte")
    b.button(text="⚡ Антилаг-Telegram", callback_data="nav:tgproxy")
    if show_trial:
        b.button(text="🎁 Пробный период 5 дней", callback_data="trial:start")
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


def kb_pay(*, price_rub: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ Оплатить {int(price_rub)} ₽", callback_data="pay:buy:1m")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_vpn(*, show_my_config: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📖 Инструкция", callback_data="vpn:guide")
    b.button(text="👨‍👩‍👧‍👦 Семейная группа", callback_data="vpn:family")
    if show_my_config:
        b.button(text="📌 Мой конфиг", callback_data="vpn:my")
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
    b.button(text="🕒 WG grace (24ч)", callback_data="admin:vpn:grace")
    b.button(text="➕ Админу доп. устройства", callback_data="admin:vpn:extra")
    b.button(text="👤 Все пользователи", callback_data="admin:users")
    b.button(text="🔎 Карточка пользователя", callback_data="admin:user:inspect")
    b.button(text="💰 Цена места семьи", callback_data="admin:family_price")
    b.button(text="👥 Активные VPN-профили", callback_data="admin:vpn:active_profiles")
    b.button(text="🗂 Пользователи по серверам", callback_data="admin:vpn:server_users")
    b.button(text="📶 Активные LTE-профили", callback_data="admin:vpn:active_lte_profiles")
    b.button(text="🌐 VPN-Region профили", callback_data="admin:regionvpn:profiles")

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
    b.button(text="📶 Цена VPN LTE", callback_data="admin:lte_price")
    b.button(text="🎁 Подарить подписку", callback_data="admin:sub:gift")
    b.button(text="🎁 Подарить дни всем", callback_data="admin:sub:gift_days:all")
    b.button(text="🎁 Подарить дни активным", callback_data="admin:sub:gift_days:active")
    b.button(text="🧪 Тестовый пир Server #2", callback_data="admin:vpn:test_peer:2")
    b.button(text="📣 Рассылка всем", callback_data="admin:broadcast:all")
    b.button(text="🟢 Рассылка с подпиской", callback_data="admin:broadcast:paid")
    b.button(text="⚪️ Рассылка без подписки", callback_data="admin:broadcast:unpaid")
    b.button(text="✉️ Сообщение пользователю", callback_data="admin:broadcast:one")
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


def kb_lte_vpn(*, has_access: bool, activation_rub: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="ℹ️ Что это?", callback_data="vpn:lte:about")
    if has_access:
        b.button(text="📋 Скопировать в Happ+", callback_data="vpn:lte:install")
        b.button(text="♻️ Получить новый конфиг", callback_data="vpn:lte:reset")
    else:
        b.button(text=f"💳 Активировать за {int(activation_rub)} ₽", callback_data="vpn:lte:pay")
    b.button(text="⬅️ Назад", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()


def kb_lte_main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Главное меню", callback_data="nav:home")
    b.adjust(1)
    return b.as_markup()
