@router.callback_query(lambda c: c.data == "vpn:bundle")
async def on_vpn_bundle(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id

    async with session_scope() as session:
        sub = await get_subscription(session, tg_id)
        if not _is_sub_active(sub.end_at):
            await cb.answer("Подписка не активна", show_alert=True)
            return

        try:
            # пробуем создать peer на сервере
            peer = await vpn_service.ensure_peer(session, tg_id)
            await session.commit()
        except Exception as e:
            # ❗ ВАЖНО: НЕ падаем, а продолжаем
            await cb.answer(
                "⚠️ VPN сервер временно недоступен.\n"
                "Конфиг сгенерирован, попробуй подключиться позже.",
                show_alert=True,
            )

            # генерим локальный peer БЕЗ SSH
            peer = {
                "client_private_key_plain": vpn_service._gen_keys()[0]
                if hasattr(vpn_service, "_gen_keys")
                else None,
                "client_ip": vpn_service._alloc_ip(tg_id),
            }

    if not peer.get("client_private_key_plain"):
        await cb.answer("❌ Не удалось сгенерировать ключ", show_alert=True)
        return

    # строим конфиг
    conf_text = vpn_service.build_wg_conf(peer, user_label=str(tg_id))

    # QR
    qr_img = qrcode.make(conf_text)
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    buf.seek(0)

    conf_file = BufferedInputFile(conf_text.encode(), filename="wg.conf")
    qr_file = BufferedInputFile(buf.getvalue(), filename="wg.png")

    msg_conf = await cb.message.answer_document(
        document=conf_file,
        caption=f"WireGuard конфиг. Будет удалён через {settings.auto_delete_seconds} сек.",
    )
    msg_qr = await cb.message.answer_photo(
        photo=qr_file,
        caption="QR для WireGuard",
    )

    await cb.answer()

    async def _cleanup():
        await asyncio.sleep(settings.auto_delete_seconds)
        for m in (msg_conf, msg_qr):
            try:
                await m.delete()
            except Exception:
                pass
        try:
            await cb.message.edit_text("Главное меню:", reply_markup=kb_main())
        except Exception:
            pass

    asyncio.create_task(_cleanup())
