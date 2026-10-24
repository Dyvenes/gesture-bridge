package com.dyvenes.gesturebridge

import android.content.Context
import android.os.Build

/** Адрес брокера, токен и имя устройства. Больше хранить нечего. */
class Settings(context: Context) {

    private val prefs = context.getSharedPreferences("bridge", Context.MODE_PRIVATE)

    var broker: String
        get() = prefs.getString("broker", "") ?: ""
        set(value) = prefs.edit().putString("broker", value.trim().trimEnd('/')).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) = prefs.edit().putString("token", value.trim()).apply()

    var deviceName: String
        get() = prefs.getString("name", null) ?: (Build.MODEL ?: "Телефон")
        set(value) = prefs.edit().putString("name", value.trim()).apply()

    /** Поднимать ли сервис после перезагрузки телефона. */
    var autoStart: Boolean
        get() = prefs.getBoolean("autoStart", true)
        set(value) = prefs.edit().putBoolean("autoStart", value).apply()

    val configured: Boolean
        get() = broker.isNotEmpty() && token.isNotEmpty()
}
