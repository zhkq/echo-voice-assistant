package com.echo.contact.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import com.echo.contact.data.dto.ApiErrorResponse
import com.google.gson.Gson
import retrofit2.HttpException
import retrofit2.Response

/** 通用基类：持有 Application 上下文，并把后端错误体翻译成人话。 */
open class BaseViewModel(app: Application) : AndroidViewModel(app) {

    private fun errorBodySafe(body: okhttp3.ResponseBody?): String {
        return try {
            body?.string()?.take(2000).orEmpty()
        } catch (_: Exception) {
            ""
        }
    }

    /**
     * 后端一律用 FastAPI 的 `{"detail": "..."}` 报错（例如配对码无效是 403 + detail），
     * 所以优先取 `detail`；取不到再退回原始文本，最后才用状态码兜底。
     */
    private fun humanMessage(raw: String, code: Int): String {
        if (raw.isNotBlank()) {
            val detail = try {
                Gson().fromJson(raw, ApiErrorResponse::class.java)?.detail
            } catch (_: Exception) {
                null
            }
            if (!detail.isNullOrBlank()) return detail
            return "$raw (HTTP $code)"
        }
        return "请求失败 (HTTP $code)"
    }

    /** 从非 2xx 的 Response 里提取人话错误。 */
    protected fun parseError(resp: Response<*>): String =
        humanMessage(errorBodySafe(resp.errorBody()), resp.code())

    /** 从 HttpException 里提取人话错误。 */
    protected fun parseHttp(e: HttpException): String =
        humanMessage(errorBodySafe(e.response()?.errorBody()), e.code())
}
