package com.dyvenes.gesturebridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Поднимает сервис после перезагрузки телефона.
 *
 * Запускаем только если устройство уже привязано: иначе сервис встанет
 * с пустыми настройками, повисит уведомлением и ничего не сделает.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        if (!Settings(context).configured) return
        if (!Settings(context).autoStart) return

        BridgeService.start(context)
    }
}
